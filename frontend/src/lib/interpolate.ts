/**
 * 纯函数插值器：与 i18next 的 `{var}` / `{{var}}` 语法保持同一语义。
 *
 * 单独成文件的原因：`lib/t.ts` 在模块加载时就会 import `i18n`，
 * 而 i18n 初始化要访问 `localStorage` / `document`，在 node 测试环境下不可用。
 * 把纯逻辑拆出来，测试可以直接覆盖，不需要为跑一条断言而装整套 DOM 环境。
 */

export type Vars = Record<string, unknown> | undefined

/**
 * 替换 `{name}` 与 `{{name}}`；缺失的变量保留原样，便于发现调用方漏传。
 * 值为 `null` / `undefined` 时按空串处理，`0` 与 `false` 正常渲染。
 */
export function interpolate(text: string, vars: Vars): string {
  if (!vars) return text
  return text.replace(
    /\{\{\s*([\w.]+)\s*\}\}|\{\s*([\w.]+)\s*\}/g,
    (whole: string, doubleKey?: string, singleKey?: string) => {
      const name = doubleKey ?? singleKey
      if (!name || !(name in vars)) return whole
      const value = vars[name]
      return value === null || value === undefined ? '' : String(value)
    },
  )
}
