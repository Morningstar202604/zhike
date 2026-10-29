import React, { Component } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

/*
 * Markdown 渲染器。
 *
 * 两个关键设计：
 * 1. **语法高亮按需加载**：highlight.js 的 12 种语言 + core 约 145KB（gzip 48KB），
 *    而实际网页里大多数消息不含代码块。改前它是同步 import，等于每个访客首屏
 *    都要为「可能出现的代码块」付费。现在只在首次真的遇到 <pre><code> 时才
 *    动态 import，纯聊天场景零成本。
 * 2. **XSS 防线**：marked 输出后必须过 DOMPurify（默认配置就够，
 *    这里显式白名单是为了禁止 style/iframe 等可被用来做点击劫持的标签）。
 */

marked.setOptions({ gfm: true, breaks: true })

const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'span',
]

function render(source) {
  const raw = marked.parse(source || '')
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class'],
  })
}

/** @type {Promise<any>|null} */
let hljsPromise = null
/** @type {any} */
let hljsInstance = null

/** 首次需要高亮时才加载 highlight.js（含常用语言包）。 */
async function loadHljs() {
  if (hljsInstance) return hljsInstance
  if (!hljsPromise) {
    hljsPromise = (async () => {
      const [core, langs] = await Promise.all([
        import('highlight.js/lib/core'),
        Promise.all([
          import('highlight.js/lib/languages/javascript'),
          import('highlight.js/lib/languages/typescript'),
          import('highlight.js/lib/languages/python'),
          import('highlight.js/lib/languages/bash'),
          import('highlight.js/lib/languages/json'),
          import('highlight.js/lib/languages/sql'),
          import('highlight.js/lib/languages/css'),
          import('highlight.js/lib/languages/xml'),
          import('highlight.js/lib/languages/go'),
          import('highlight.js/lib/languages/java'),
          import('highlight.js/lib/languages/yaml'),
          import('highlight.js/lib/languages/markdown'),
        ]),
      ])
      const hljs = core.default
      const [js, ts, py, sh, json, sql, css, xml, go, java, yaml, md] = langs.map((m) => m.default)
      const reg = [
        ['javascript', js], ['js', js], ['typescript', ts], ['ts', ts],
        ['python', py], ['py', py], ['bash', sh], ['sh', sh], ['shell', sh],
        ['json', json], ['sql', sql], ['css', css], ['html', xml], ['xml', xml],
        ['go', go], ['java', java], ['yaml', yaml], ['yml', yaml],
        ['markdown', md], ['md', md],
      ]
      for (const [name, lang] of reg) hljs.registerLanguage(name, lang)
      hljsInstance = hljs
      return hljsInstance
    })().catch(() => null)
  }
  return hljsPromise
}

function decorateCodeBlocks(root) {
  if (!root) return
  root.querySelectorAll('pre').forEach((pre) => {
    if (pre.querySelector('.code-copy')) return
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'code-copy'
    btn.textContent = '复制'
    btn.addEventListener('click', () => {
      const code = pre.querySelector('code')
      const text = code ? code.textContent : pre.textContent
      navigator.clipboard?.writeText(text || '').then(() => {
        btn.textContent = '已复制'
        setTimeout(() => { btn.textContent = '复制' }, 1500)
      }).catch(() => {
        btn.textContent = '复制失败'
        setTimeout(() => { btn.textContent = '复制' }, 1500)
      })
    })
    pre.appendChild(btn)
  })
}

async function highlightIn(root) {
  if (!root) return
  const blocks = root.querySelectorAll('pre code')
  if (!blocks.length) return
  const hljs = await loadHljs()
  if (!hljs) return
  blocks.forEach((el) => {
    if (el.dataset.highlighted) return
    const cls = [...el.classList].find((c) => c.startsWith('language-'))
    const lang = cls ? cls.slice(9) : ''
    try {
      const out = lang && hljs.getLanguage(lang)
        ? hljs.highlight(el.textContent, { language: lang, ignoreIllegals: true })
        : hljs.highlightAuto(el.textContent)
      el.innerHTML = out.value
      el.dataset.highlighted = '1'
      el.classList.add('hljs')
    } catch {
      el.dataset.highlighted = '1'
    }
  })
}

export default class Markdown extends Component {
  constructor(props) {
    super(props)
    this.root = React.createRef()
    this.html = render(props.text)
  }

  componentDidMount() {
    void highlightIn(this.root.current)
    decorateCodeBlocks(this.root.current)
  }

  shouldComponentUpdate(next) {
    if (next.text === this.props.text) return false
    this.html = render(next.text)
    return true
  }

  componentDidUpdate() {
    void highlightIn(this.root.current)
    decorateCodeBlocks(this.root.current)
  }

  handleClick = (e) => {
    const a = e.target.closest('a')
    if (!a) return
    const href = a.getAttribute('href') || ''
    if (!/^https?:/i.test(href)) return
    e.preventDefault()
    window.open(href, '_blank', 'noopener,noreferrer')
  }

  render() {
    return (
      <div
        ref={this.root}
        className="md-body"
        onClick={this.handleClick}
        dangerouslySetInnerHTML={{ __html: this.html }}
      />
    )
  }
}
