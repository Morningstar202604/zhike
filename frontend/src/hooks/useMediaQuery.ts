import { useEffect, useState } from 'react'

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia(query).matches : false,
  )

  useEffect(() => {
    const mql = window.matchMedia(query)
    const handler = () => setMatches(mql.matches)
    handler()
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [query])

  return matches
}

/**
 * 便捷 Hook：调用方必须在组件顶层调用（不能让 `useXxx` 藏在一个普通函数里，
 * 那样 React 无法确定调用顺序，条件渲染时会报 "Rendered more hooks than
 * during the previous render"）。所以这里导出的是 Hook 而不是普通函数。
 */
export function useIsMobile(): boolean {
  return useMediaQuery('(max-width: 639px)')
}

export function useIsTablet(): boolean {
  return useMediaQuery('(max-width: 899px)')
}
