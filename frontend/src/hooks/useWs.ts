import { useEffect, useRef, useState } from 'react'

export interface WsMessage {
  type: string
  [k: string]: unknown
}

/** 指数退避重连：250ms 起，翻倍到 15s 上限，加 ±20% 抖动避免群断后同时重连。 */
function nextDelay(attempt: number): number {
  const base = Math.min(250 * 2 ** attempt, 15_000)
  return Math.round(base * (0.8 + Math.random() * 0.4))
}

export function useWs(
  url: string | null,
  onMessage: (p: WsMessage) => void,
  enabled = true,
  subprotocols?: string[],
): { state: 'connecting' | 'online' | 'offline'; close: () => void } {
  const [state, setState] = useState<'connecting' | 'online' | 'offline'>(url ? 'connecting' : 'offline')
  const wsRef = useRef<WebSocket | null>(null)
  const closedRef = useRef(false)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attemptRef = useRef(0)
  // 回调与子协议放 ref：避免调用方每次渲染传新函数/新数组导致连接被重建。
  // 必须在 effect 里同步，不能在渲染期写 ref（并发渲染下渲染可能被丢弃，
  // 提前写 ref 会留下"从未提交的状态"）。
  const onMsgRef = useRef(onMessage)
  const subRef = useRef(subprotocols)

  useEffect(() => {
    onMsgRef.current = onMessage
  }, [onMessage])

  useEffect(() => {
    subRef.current = subprotocols
  }, [subprotocols])

  useEffect(() => {
    if (!enabled || !url) return
    closedRef.current = false
    attemptRef.current = 0

    const connect = () => {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const host = window.location.host
      const subs = subRef.current
      const socket = subs?.length
        ? new WebSocket(`${proto}://${host}${url}`, subs)
        : new WebSocket(`${proto}://${host}${url}`)
      wsRef.current = socket
      setState('connecting')
      socket.onopen = () => {
        attemptRef.current = 0
        setState('online')
      }
      socket.onclose = (ev) => {
        setState('offline')
        // 4401 = 鉴权失败：重连也没用，停止重试避免无限打服务端
        if (closedRef.current || ev.code === 4401) return
        const delay = nextDelay(attemptRef.current++)
        retryRef.current = setTimeout(connect, delay)
      }
      socket.onerror = () => socket.close()
      socket.onmessage = (e) => {
        try {
          onMsgRef.current(JSON.parse(e.data) as WsMessage)
        } catch {
          /* 忽略畸形帧，不让一条坏消息打断整个连接 */
        }
      }
    }
    connect()
    return () => {
      closedRef.current = true
      if (retryRef.current) clearTimeout(retryRef.current)
      wsRef.current?.close()
    }
  }, [url, enabled])

  const close = () => {
    closedRef.current = true
    if (retryRef.current) clearTimeout(retryRef.current)
    wsRef.current?.close()
    setState('offline')
  }

  return { state, close }
}
