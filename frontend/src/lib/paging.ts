/** 会话列表分页的纯函数：单独抽出便于单测，页面组件只做渲染。 */

export const PAGE_SIZE = 30

export function pageCount(total: number, size = PAGE_SIZE): number {
  if (!Number.isFinite(total) || total <= 0) return 1
  return Math.max(1, Math.ceil(total / size))
}

export function clampPage(page: number, total: number, size = PAGE_SIZE): number {
  const max = pageCount(total, size) - 1
  if (!Number.isFinite(page) || page < 0) return 0
  return Math.min(Math.floor(page), max)
}

export function pageOffset(page: number, total: number, size = PAGE_SIZE): number {
  return clampPage(page, total, size) * size
}
