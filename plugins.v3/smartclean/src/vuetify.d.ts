import 'vue'
import type * as Components from 'vuetify/components'

declare module 'vue' {
  export interface GlobalComponents {
    VAlert: typeof Components.VAlert
    VBtn: typeof Components.VBtn
    VBtnToggle: typeof Components.VBtnToggle
    VChip: typeof Components.VChip
    VCheckbox: typeof Components.VCheckbox
    VCombobox: typeof Components.VCombobox
    VCol: typeof Components.VCol
    VCronField: {
      new (): {
        $props: {
          modelValue?: string | null
          clearable?: boolean
          label?: string
          hint?: string
          persistentHint?: boolean
          hideDetails?: boolean | 'auto'
        }
      }
    }
    VRow: typeof Components.VRow
    VSelect: typeof Components.VSelect
    VSwitch: typeof Components.VSwitch
    VTab: typeof Components.VTab
    VTabs: typeof Components.VTabs
    VTextField: typeof Components.VTextField
    VTextarea: typeof Components.VTextarea
  }
}
