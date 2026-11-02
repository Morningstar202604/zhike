/**
 * 无 Hook 的翻译函数：中文原文即 key，缺英文时原样返回中文。
 *
 * 为什么不用 useTranslation：页面里有大量「纯文本节点」，逐组件加 Hook 改动面太大。
 * 语言切换时 setLang() 会重载页面，保证所有模块级文本一起更新。
 *
 * 为什么要自己补插值：i18next 只在**命中资源**时替换 `{var}` / `{{var}}`。
 * 本项目以中文原文作 key，未翻译的句子会 miss，此时 i18next 原样返回
 * `schema v{v}` 这类占位符——界面上会直接显示花括号（已在数据概览页实际出现过）。
 * 所以 miss 时在这里补一次同语义的本地插值，保证「没翻译」最多是中文，
 * 而不是坏掉的花括号文本。
 */
import i18n from '../i18n'
import { interpolate, type Vars } from './interpolate'

export function t(text: string, vars?: Vars): string {
  if (!text) return text
  const translated = i18n.t(text, vars) as string
  // 命中资源时 i18next 已完成插值；miss 时它会返回原文（含花括号），这里补一次。
  return translated === text ? interpolate(text, vars) : translated
}

export default t
