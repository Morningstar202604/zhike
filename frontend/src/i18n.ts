/**
 * 国际化：中文原文即 key（gettext 式 msgid），英文资源按同一中文键查表。
 *
 * 为什么用中文当 key：现有代码里已有 600+ 处中文文案，逐条改造成抽象 key
 * 既容易漏、也会让 diff 无法 review；而「缺翻译时回退到中文」天然不会白屏。
 * 走 i18next 而不是自研：语言切换/插值/回退/复数都是它已经在解决的成熟问题。
 */
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './locales/en'

export const LANGS = [
  { code: 'zh', label: '中' },
  { code: 'en', label: 'EN' },
]

const saved = typeof localStorage !== 'undefined' ? localStorage.getItem('zhike-lang') : ''
const initial = saved === 'en' ? 'en' : 'zh'

i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: {} },
    en: { translation: en },
  },
  lng: initial,
  fallbackLng: 'zh',
  // 中文句子本身含标点、空格与斜杠，不能被当成 key 分隔符
  keySeparator: false,
  nsSeparator: false,
  returnNull: false,
  interpolation: { escapeValue: false },
})

export function setLang(code: string) {
  const next = code === 'en' ? 'en' : 'zh'
  localStorage.setItem('zhike-lang', next)
  document.documentElement.lang = next === 'en' ? 'en' : 'zh-CN'
  // 页面里的纯文本节点走模块级 t()，没有订阅语言变化；重载一次最省心，
  // 也避免半个界面英文半个界面中文的中间态。
  window.location.reload()
}

document.documentElement.lang = initial === 'en' ? 'en' : 'zh-CN'

export default i18n
