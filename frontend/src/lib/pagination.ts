import { useState } from 'react'
import { PAGE_SIZE as SHARED_PAGE_SIZE } from './paging'

const PAGE_SIZE = SHARED_PAGE_SIZE

/** 受控分页列表：只渲染当前页，避免长表全量重渲染。
 *
 * 页码越界不靠 effect 纠正（那会多渲染一轮并触发 lint 警告），
 * 直接在读取时收敛：clamp 是纯函数，渲染期算出来即可。
 */
export function usePagination<T>(items: T[]) {
  const [page, setPage] = useState(0)
  const maxPage = Math.max(0, Math.ceil(items.length / PAGE_SIZE) - 1)
  const current = Math.min(page, maxPage)
  const pageItems = items.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE)
  return {
    page: current,
    maxPage,
    pageItems,
    setPage,
    hasPages: items.length > PAGE_SIZE,
    total: items.length,
  }
}
