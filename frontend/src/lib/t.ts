/**
 * 无 Hook 的翻译函数：中文原文即 key，缺英文时原样返回中文。
 *
 * 为什么不用 useTranslation：页面里有大量「纯文本节点」，逐组件加 Hook 改动面太大。
 * 语言切换时 setLang() 会重载页面，保证所有模块级文本一起更新。
 */
import i18n from '../i18n'

export function t(text: string, vars?: Record<string, unknown>): string {
  if (!text) return text
  return i18n.t(text, vars) as string
}

export default t
