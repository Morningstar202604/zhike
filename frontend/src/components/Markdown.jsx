import React, { Component } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import sql from 'highlight.js/lib/languages/sql'
import css from 'highlight.js/lib/languages/css'
import xml from 'highlight.js/lib/languages/xml'
import go from 'highlight.js/lib/languages/go'
import java from 'highlight.js/lib/languages/java'
import yaml from 'highlight.js/lib/languages/yaml'
import markdown from 'highlight.js/lib/languages/markdown'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('ts', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('py', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('css', css)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('go', go)
hljs.registerLanguage('java', java)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('yml', yaml)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('md', markdown)

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

function highlightIn(root) {
  if (!root) return
  root.querySelectorAll('pre code').forEach((el) => {
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
    highlightIn(this.root.current)
    decorateCodeBlocks(this.root.current)
  }

  shouldComponentUpdate(next) {
    if (next.text === this.props.text) return false
    this.html = render(next.text)
    return true
  }

  componentDidUpdate() {
    highlightIn(this.root.current)
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
