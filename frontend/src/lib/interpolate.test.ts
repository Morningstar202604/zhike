import { describe, expect, it } from 'vitest'
import { interpolate } from './interpolate'

/**
 * 回归背景：数据概览页曾显示 `schema v{v}`。
 * 本项目用中文原文当 i18next key，未翻译的句子 miss 后原样返回，
 * 花括号占位符就露在界面上。t() 必须在 miss 时自己完成插值。
 */
describe('interpolate()', () => {
  it('替换单花括号占位符', () => {
    expect(interpolate('schema v{v}', { v: 16 })).toBe('schema v16')
  })

  it('替换双花括号占位符', () => {
    expect(interpolate('已清理 {{n}} 条', { n: 3 })).toBe('已清理 3 条')
  })

  it('多个占位符', () => {
    expect(interpolate('{a} 与 {b}', { a: 'x', b: 'y' })).toBe('x 与 y')
  })

  it('无变量时原样返回', () => {
    expect(interpolate('会话', undefined)).toBe('会话')
  })

  it('缺失变量保留占位符，便于发现调用方漏传', () => {
    expect(interpolate('schema v{v}', {})).toBe('schema v{v}')
  })

  it('值为 0 时正常渲染（不能被当成 falsy 丢弃）', () => {
    expect(interpolate('剩 {n} 条', { n: 0 })).toBe('剩 0 条')
  })

  it('值为 null/undefined 时渲染为空串', () => {
    expect(interpolate('x={x}', { x: null })).toBe('x=')
  })

  it('没有占位符的文本不受影响', () => {
    expect(interpolate('纯文本', { unused: 1 })).toBe('纯文本')
  })
})
