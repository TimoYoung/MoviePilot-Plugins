"""Google Drive 插件的静态契约与宿主编排桩测试。

AST/JSON 用例不依赖 MoviePilot；实例行为用例用最小 ``app.*`` 桩导入插件，
适合在插件仓本地 CI/无宿主环境中执行。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = REPO_ROOT / "plugins.v3" / "gdrivestorage" / "__init__.py"
README_PATH = REPO_ROOT / "plugins.v3" / "gdrivestorage" / "README.md"
PACKAGE_PATH = REPO_ROOT / "package.v3.json"
ICONS_DIR = REPO_ROOT / "icons"

PLUGIN_PACKAGE = "gdrivestorage_under_test"
FORBIDDEN_IMPORT_PREFIXES = ("app.core", "app.helper", "app.utils", "app.db.models")
STUB_MODULE_NAMES = (
    "app",
    "app.schemas",
    "app.schemas.file",
    "app.schemas.types",
    "app.sdk",
    "app.sdk.events",
    "app.sdk.logging",
    "app.sdk.plugin",
    "app.sdk.services",
    PLUGIN_PACKAGE,
)


@pytest.fixture(scope="module", autouse=True)
def _restore_sys_modules():
    """模块测试结束后恢复宿主/插件模块，避免污染其它测试。"""
    saved = {name: sys.modules.get(name) for name in STUB_MODULE_NAMES}
    yield
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module

REQUIRED_MODULE_METHODS = {
    "list_files",
    "any_files",
    "create_folder",
    "get_folder",
    "download_file",
    "upload_file",
    "delete_file",
    "rename_file",
    "get_file_item",
    "get_file_item_strict",
    "get_parent_item",
    "snapshot_storage",
    "storage_manage",
}

REQUIRED_FORM_KEYS = {
    "enabled",
    "display_name",
    "register_storage",
    "client_id",
    "client_secret",
    "refresh_token",
    "redirect_uri",
    "oauth_code",
    "root_folder_id",
    "root_path",
    "use_shared_drive",
    "shared_drive_id",
    "delete_mode",
    "chunk_size_mb",
    "timeout",
}

ENVELOPE_KEYS = {"success", "message", "data"}


def _tree() -> ast.Module:
    """解析插件源码。"""
    return ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    """查找指定类定义。"""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _method_node(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    """查找类中的指定方法。"""
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found")


def _class_attr(class_node: ast.ClassDef, name: str) -> Optional[ast.AST]:
    """读取类级赋值。"""
    for node in class_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    return None


def _dict_keys(node: ast.Dict) -> Set[str]:
    """读取字典字面量的字符串键。"""
    keys: Set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _return_dicts(func: ast.FunctionDef) -> Iterable[ast.Dict]:
    """遍历函数体内直接 return 的字典字面量。"""
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            yield node.value


def test_get_module_declares_all_storage_methods() -> None:
    """get_module 必须声明全部存储模块方法。"""
    class_node = _class_node(_tree(), "GDriveStorage")
    get_module = _method_node(class_node, "get_module")
    returned = [node for node in _return_dicts(get_module)]
    assert returned, "get_module must return a dict literal"
    keys = set()
    for node in returned:
        keys.update(_dict_keys(node))
    assert REQUIRED_MODULE_METHODS <= keys


def test_get_form_defaults_are_complete() -> None:
    """配置表单默认值必须覆盖全部配置键。"""
    class_node = _class_node(_tree(), "GDriveStorage")
    get_form = _method_node(class_node, "get_form")
    tuple_returns = [
        node.value for node in ast.walk(get_form)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
    ]
    assert tuple_returns, "get_form must return a tuple"
    default_dict = tuple_returns[0].elts[1]
    assert isinstance(default_dict, ast.Dict)
    assert set(_dict_keys(default_dict)) == REQUIRED_FORM_KEYS


def test_page_api_returns_exactly_three_keys() -> None:
    """get_api 声明的页面端点必须返回三键 envelope。"""
    class_node = _class_node(_tree(), "GDriveStorage")
    get_api = _method_node(class_node, "get_api")
    list_returns = [
        node.value for node in ast.walk(get_api)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.List)
    ]
    assert list_returns, "get_api must return a list literal"
    endpoints: List[str] = []
    for element in list_returns[0].elts:
        assert isinstance(element, ast.Dict)
        for key, value in zip(element.keys, element.values):
            if isinstance(key, ast.Constant) and key.value == "endpoint":
                assert isinstance(value, ast.Attribute)
                endpoints.append(value.attr)
    assert endpoints, "get_api must declare at least one endpoint"
    for endpoint in endpoints:
        method = _method_node(class_node, endpoint)
        returns = list(_return_dicts(method))
        assert returns, f"{endpoint} must return a dict envelope"
        for node in returns:
            assert _dict_keys(node) == ENVELOPE_KEYS, (
                f"{endpoint} envelope keys must be {ENVELOPE_KEYS}, got {_dict_keys(node)}"
            )


def test_oauth_callback_is_anonymous() -> None:
    """OAuth 回调端点必须允许匿名访问，并在源码中校验 state。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert '"path": "/oauth/callback"' in source
    assert '"allow_anonymous": True' in source
    assert "oauth_state" in source
    assert "state != saved.get" in source


def test_package_metadata_matches_plugin_version() -> None:
    """package.v3.json 的版本、历史和图标必须与插件一致。"""
    class_node = _class_node(_tree(), "GDriveStorage")
    version_node = _class_attr(class_node, "plugin_version")
    assert isinstance(version_node, ast.Constant)
    version = version_node.value
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    entry = package["GDriveStorage"]
    assert entry["version"] == version
    assert entry["icon"]
    assert (ICONS_DIR / entry["icon"]).is_file()
    assert next(iter(entry["history"])) == f"v{version}"


def test_plugin_directory_and_class_name_are_consistent() -> None:
    """插件目录必须是类名的小写形式。"""
    class_node = _class_node(_tree(), "GDriveStorage")
    assert PLUGIN_PATH.parent.name == class_node.name.lower()


# ----------------------------------------------------------------------
# 宿主桩：在不安装 MoviePilot 的环境下导入插件并执行实例方法
# ----------------------------------------------------------------------
class _StubFileItem:
    """宿主 FileItem 的最小替身。"""

    def __init__(
            self,
            storage: Optional[str] = None,
            path: Optional[str] = None,
            type: Optional[str] = None,
            fileid: Optional[str] = None,
            name: Optional[str] = None,
            extension: Optional[str] = None,
            size: Optional[int] = None,
            **kwargs: Any,
    ) -> None:
        self.storage = storage
        self.path = path
        self.type = type
        self.fileid = fileid
        self.name = name
        self.extension = extension
        self.size = size
        for key, value in kwargs.items():
            setattr(self, key, value)


class _StubFileItemQueryResult:
    """宿主 FileItemQueryResult 的最小替身。"""

    def __init__(self, item: Optional[_StubFileItem] = None) -> None:
        self.item = item


class _StubStorageUsage:
    """宿主 StorageUsage 的最小替身。"""

    def __init__(self, total: float = 0.0, available: float = 0.0) -> None:
        self.total = total
        self.available = available


class _StubChainEventType(Enum):
    """宿主链式事件枚举的最小替身。"""

    StorageOperSelection = "storage.operation"


class _StubPluginBase:
    """宿主 _PluginBase 的最小替身，提供配置与数据持久化接口。"""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._saved_config: Dict[str, Any] = {}

    def update_config(self, config: Dict[str, Any], plugin_id: Optional[str] = None) -> bool:
        """模拟配置写回。"""
        self._saved_config = dict(config)
        return True

    def get_config(self, plugin_id: Optional[str] = None) -> Dict[str, Any]:
        """模拟配置读取。"""
        return dict(self._saved_config)

    def save_data(self, key: str, value: Any, plugin_id: Optional[str] = None) -> None:
        """模拟插件数据保存。"""
        self._data[key] = value

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """模拟插件数据读取。"""
        return self._data.get(key) if key else dict(self._data)

    def del_data(self, key: str, plugin_id: Optional[str] = None) -> Any:
        """模拟插件数据删除。"""
        return self._data.pop(key, None)


class _StubEventManager:
    """宿主事件管理器替身，装饰器保持原函数不变。"""

    def register(self, etype: Any, priority: Optional[int] = None):
        """返回不改变处理器签名的装饰器。"""
        def decorator(handler):
            return handler
        return decorator


class _StubStorageHelper:
    """宿主 StorageHelper 替身，记录注册行为。"""

    _storages: Dict[str, Any] = {}
    added: List[tuple] = []
    updated: List[tuple] = []

    def get_storage(self, storage: str) -> Optional[Any]:
        """返回已登记的存储配置。"""
        return self._storages.get(storage)

    def get_storagies(self) -> List[Any]:
        """返回全部存储配置。"""
        return list(self._storages.values())

    def add_storage(self, storage: str, name: str, conf: Dict[str, Any]) -> None:
        """记录新增存储。"""
        self._storages[storage] = types.SimpleNamespace(
            type=storage, name=name, config=dict(conf),
        )
        self.added.append((storage, name, dict(conf)))

    def set_storage(self, storage: str, conf: Dict[str, Any]) -> None:
        """记录存储配置更新。"""
        existing = self._storages.get(storage)
        if existing is None:
            self._storages[storage] = types.SimpleNamespace(
                type=storage, name=None, config=dict(conf),
            )
        else:
            existing.config = dict(conf)
        self.updated.append((storage, dict(conf)))


def _reset_stub_storage() -> None:
    """清空存储桩状态，避免用例相互影响。"""
    _StubStorageHelper._storages = {}
    _StubStorageHelper.added = []
    _StubStorageHelper.updated = []


class _StubClient:
    """GoogleDriveClient 的最小替身。"""

    def __init__(self) -> None:
        self.close_calls = 0

    def build_auth_url(self, redirect_uri: str, state: str) -> str:
        """返回可辨识的授权链接。"""
        return f"https://accounts.example/auth?redirect_uri={redirect_uri}&state={state}"

    def close(self) -> None:
        """记录关闭次数。"""
        self.close_calls += 1


class _StubDriveClient:
    """仅用于严格查询测试的 GoogleDriveClient 替身。"""

    def __init__(self, item: Optional[Any]) -> None:
        self.item = item

    def get_item(self, path: Any) -> Optional[Any]:
        """返回预设查询结果。"""
        return self.item


def _install_host_stubs() -> None:
    """把 app.* 宿主依赖替换为最小桩，仅安装一次。"""
    if "app.schemas.file" in sys.modules and getattr(
            sys.modules["app.schemas.file"], "_plugin_contract_stub", False
    ):
        return

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    schemas_module = types.ModuleType("app.schemas")
    schemas_module.__path__ = []
    sdk_module = types.ModuleType("app.sdk")
    sdk_module.__path__ = []
    file_module = types.ModuleType("app.schemas.file")
    file_module._plugin_contract_stub = True
    types_module = types.ModuleType("app.schemas.types")
    events_module = types.ModuleType("app.sdk.events")
    logging_module = types.ModuleType("app.sdk.logging")
    plugin_module = types.ModuleType("app.sdk.plugin")
    services_module = types.ModuleType("app.sdk.services")

    file_module.FileItem = _StubFileItem
    file_module.FileItemQueryResult = _StubFileItemQueryResult
    file_module.StorageUsage = _StubStorageUsage
    types_module.ChainEventType = _StubChainEventType
    events_module.Event = object
    events_module.eventmanager = _StubEventManager()
    logging_module.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    plugin_module._PluginBase = _StubPluginBase
    services_module.StorageHelper = _StubStorageHelper

    app_module.schemas = schemas_module
    app_module.sdk = sdk_module
    sys.modules.update({
        "app": app_module,
        "app.schemas": schemas_module,
        "app.sdk": sdk_module,
        "app.schemas.file": file_module,
        "app.schemas.types": types_module,
        "app.sdk.events": events_module,
        "app.sdk.logging": logging_module,
        "app.sdk.plugin": plugin_module,
        "app.sdk.services": services_module,
    })


def _load_plugin_module() -> Any:
    """安装宿主桩后按包加载插件模块。"""
    _install_host_stubs()
    if PLUGIN_PACKAGE in sys.modules:
        return sys.modules[PLUGIN_PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PLUGIN_PACKAGE,
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def test_module_methods_return_none_for_non_gdrive_storage() -> None:
    """所有模块方法对非 gdrive 存储必须返回 None，交还宿主内置模块。"""
    module = _load_plugin_module()
    plugin = module.GDriveStorage()
    plugin._enabled = True
    plugin._provider = object()
    item = _StubFileItem(storage="u115", path="/Media")
    path = Path("/Media")

    assert plugin.list_files(item, False) is None
    assert plugin.any_files(item, [".mkv"]) is None
    assert plugin.create_folder(item, "child") is None
    assert plugin.get_folder("u115", path) is None
    assert plugin.download_file(item, Path("/tmp")) is None
    assert plugin.upload_file(item, Path("/tmp/a.mkv"), "a.mkv") is None
    assert plugin.delete_file(item) is None
    assert plugin.rename_file(item, "b.mkv") is None
    assert plugin.get_file_item("u115", path) is None
    assert plugin.get_file_item_strict("u115", path) is None
    assert plugin.get_parent_item(item) is None
    assert plugin.snapshot_storage("u115", path) is None
    assert plugin.storage_manage("u115", "usage") is None


def test_storage_oper_selection_only_sets_gdrive_when_ready() -> None:
    """事件仅在启用且适配器就绪时接管 gdrive 存储。"""
    module = _load_plugin_module()

    class EventData:
        def __init__(self, storage: str) -> None:
            self.storage = storage
            self.storage_oper = None

    class Event:
        def __init__(self, data: EventData) -> None:
            self.event_data = data

    plugin = module.GDriveStorage()
    provider = object()
    plugin._enabled = True
    plugin._provider = provider

    gdrive = EventData("gdrive")
    plugin.storage_oper_selection(Event(gdrive))
    assert gdrive.storage_oper is provider

    other = EventData("u115")
    plugin.storage_oper_selection(Event(other))
    assert other.storage_oper is None

    plugin._enabled = False
    disabled = EventData("gdrive")
    plugin.storage_oper_selection(Event(disabled))
    assert disabled.storage_oper is None

    plugin._enabled = True
    plugin._provider = None
    unauthorized = EventData("gdrive")
    plugin.storage_oper_selection(Event(unauthorized))
    assert unauthorized.storage_oper is None


def test_get_form_defaults_match_readme() -> None:
    """配置表单默认值必须与 README 及运行默认值一致。"""
    module = _load_plugin_module()
    plugin = module.GDriveStorage()
    form, defaults = plugin.get_form()
    assert isinstance(form, list)
    expected = {
        "enabled": False,
        "display_name": "Google Drive",
        "register_storage": True,
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "redirect_uri": (
            "http://localhost:3000/api/v1/plugin/GDriveStorage/oauth/callback"
        ),
        "oauth_code": "",
        "root_folder_id": "root",
        "root_path": "/",
        "use_shared_drive": False,
        "shared_drive_id": "",
        "delete_mode": "trash",
        "chunk_size_mb": 8,
        "timeout": 60,
    }
    assert defaults == expected
    assert defaults["redirect_uri"] in README_PATH.read_text(encoding="utf-8")


def test_get_page_does_not_raise_in_all_states() -> None:
    """未启用、未授权、已授权三种状态都必须能渲染详情页。"""
    module = _load_plugin_module()
    plugin = module.GDriveStorage()
    plugin._config = plugin._normalize_config({})

    plugin._enabled = False
    assert isinstance(plugin.get_page(), list)

    plugin._enabled = True
    assert isinstance(plugin.get_page(), list)

    plugin._client = _StubClient()
    plugin._config = plugin._normalize_config(
        {"client_id": "cid", "client_secret": "secret"}
    )
    assert isinstance(plugin.get_page(), list)

    plugin._config = plugin._normalize_config(
        {
            "client_id": "cid",
            "client_secret": "secret",
            "refresh_token": "refresh",
        }
    )
    assert isinstance(plugin.get_page(), list)


def test_init_plugin_registers_storage_without_authorization() -> None:
    """启用且开启注册时应在未授权状态下写入系统存储，且不重复追加。"""
    module = _load_plugin_module()
    _reset_stub_storage()
    plugin = module.GDriveStorage()
    config = {"enabled": True, "register_storage": True, "display_name": "GD"}

    plugin.init_plugin(config)
    plugin.init_plugin(config)

    assert _StubStorageHelper.added == [("gdrive", "GD", {})]
    assert ("gdrive", {}) in _StubStorageHelper.updated


def test_stop_service_is_idempotent_and_closes_client() -> None:
    """stop_service 可重复调用，且只关闭一次客户端。"""
    module = _load_plugin_module()
    plugin = module.GDriveStorage()
    client = _StubClient()
    plugin._client = client
    plugin._provider = object()

    plugin.stop_service()
    plugin.stop_service()

    assert client.close_calls == 1
    assert plugin._client is None
    assert plugin._provider is None


def test_strict_query_types_match_host_contract() -> None:
    """适配器严格查询返回 FileItem，模块方法再包装为 FileItemQueryResult。"""
    module = _load_plugin_module()
    drive_item = module.DriveItem(
        id="f1",
        name="a.mkv",
        mime_type="video/x-matroska",
        size=10,
        modified_time=1700000000.0,
        path="/a.mkv",
    )
    provider = module.GDriveStorageProvider(
        _StubDriveClient(drive_item), lambda *args, **kwargs: None,
    )
    adapter_item = provider.get_item_strict(Path("/a.mkv"))
    assert isinstance(adapter_item, _StubFileItem)
    assert not isinstance(adapter_item, _StubFileItemQueryResult)
    assert adapter_item.storage == "gdrive"
    assert adapter_item.path == "/a.mkv"

    plugin = module.GDriveStorage()
    plugin._enabled = True
    plugin._provider = provider
    module_result = plugin.get_file_item_strict("gdrive", Path("/a.mkv"))
    assert isinstance(module_result, _StubFileItemQueryResult)
    assert isinstance(module_result.item, _StubFileItem)
    assert module_result.item.path == "/a.mkv"

    absent_provider = module.GDriveStorageProvider(
        _StubDriveClient(None), lambda *args, **kwargs: None,
    )
    assert absent_provider.get_item_strict(Path("/missing")) is None
    plugin._provider = absent_provider
    absent_result = plugin.get_file_item_strict("gdrive", Path("/missing"))
    assert isinstance(absent_result, _StubFileItemQueryResult)
    assert absent_result.item is None


def test_no_forbidden_host_imports() -> None:
    """插件源码不得导入宿主内部模块。"""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(FORBIDDEN_IMPORT_PREFIXES), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES), alias.name
