import { describe, expect, it } from 'vitest'
import { PAGE_SIZE, clampPage, pageCount, pageOffset } from './paging'

describe('paging', () => {
  it('空列表也算一页', () => {
    expect(pageCount(0)).toBe(1)
    expect(pageOffset(0, 0)).toBe(0)
  })

  it('整除与余数', () => {
    expect(pageCount(30)).toBe(1)
    expect(pageCount(31)).toBe(2)
    expect(pageCount(PAGE_SIZE * 8)).toBe(8)
  })

  it('越界页码夹紧到最后一页', () => {
    expect(clampPage(99, 31)).toBe(1)
    expect(clampPage(-3, 31)).toBe(0)
    expect(pageOffset(99, 31)).toBe(30)
  })

  it('非法 total 不产生 NaN offset', () => {
    expect(pageOffset(0, Number.NaN)).toBe(0)
  })
})
