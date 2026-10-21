import React, { createContext, useContext, useState, useCallback } from 'react'
import type { ReactNode } from 'react'

type ToastKind = 'success' | 'error' | 'info'
type DialogState = {
  mode: 'confirm' | 'prompt'
  title: string
  message: string
  confirmText: string
  danger: boolean
  value?: string
  // 各分支 resolve 的类型不同（bool / string|null），这里取宽松签名，调用点自己收窄
  resolve: (v: any) => void
} | null

interface FeedbackApi {
  toast: { success: (m: string) => void; error: (m: string) => void; info: (m: string) => void }
  confirm: (message: string, options?: { title?: string; confirmText?: string; danger?: boolean }) => Promise<boolean>
  prompt: (message: string, defaultValue?: string, options?: { title?: string; confirmText?: string }) => Promise<string | null>
}

/**
 * 轻量 Toast + 确认框：替代 window.alert / window.confirm / window.prompt。
 *
 * 原生弹窗的问题不是"丑"——它会阻塞整个渲染线程，自动化测试也无法稳定处理，
 * 而且样式与产品完全脱节。这里用 Portal 化的固定层，支持三种语义：
 *   toast.success / error / info   一次性提示，2.6s 自动消失
 *   confirm(message) -> Promise<bool>  二次确认（删除等破坏性操作）
 *   prompt(message, defaultValue) -> Promise<string|null>  需要输入的场景（拒绝审批填原因）
 */
const Ctx = createContext<FeedbackApi | null>(null)

export function useFeedback() {
  return useContext(Ctx) as FeedbackApi
}

let seq = 0

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<{ id: number; kind: ToastKind; message: string }[]>([])
  const [dialog, setDialog] = useState<DialogState>(null)

  const push = useCallback((kind: ToastKind, message: string, ttl = 2600) => {
    const id = ++seq
    setToasts((list) => [...list, { id, kind, message }])
    if (ttl > 0) {
      setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), ttl)
    }
    return id
  }, [])

  const api = React.useMemo<FeedbackApi>(() => ({
    toast: {
      success: (m) => push('success', m),
      error: (m) => push('error', m, 4200),
      info: (m) => push('info', m),
    },
    confirm: (message, options = {}) => new Promise((resolve) => {
      setDialog({
        mode: 'confirm',
        message,
        title: options.title || '请确认',
        confirmText: options.confirmText || '确定',
        danger: options.danger !== false,
        resolve,
      })
    }),
    prompt: (message, defaultValue = '', options = {}) => new Promise((resolve) => {
      setDialog({
        mode: 'prompt',
        message,
        title: options.title || '请输入',
        confirmText: options.confirmText || '提交',
        danger: false,
        value: defaultValue,
        resolve,
      })
    }),
  }), [push])

  const close = (result: any) => {
    if (dialog) dialog.resolve(result)
    setDialog(null)
  }

  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} role="status">
            {t.message}
          </div>
        ))}
      </div>
      {dialog && (
        <div className="dlg-mask" onClick={() => close(dialog.mode === 'confirm' ? false : null)}>
          <div className="dlg" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <div className="dlg-title">{dialog.title}</div>
            <div className="dlg-msg">{dialog.message}</div>
            {dialog.mode === 'prompt' && (
              <input
                autoFocus
                className="dlg-input"
                value={dialog.value || ''}
                onChange={(e) => setDialog((d) => (d ? { ...d, value: e.target.value } : d))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') close(dialog.value || '')
                  if (e.key === 'Escape') close(null)
                }}
              />
            )}
            <div className="dlg-actions">
              <button className="btn secondary sm" onClick={() => close(dialog.mode === 'confirm' ? false : null)}>
                取消
              </button>
              <button
                className={`btn sm ${dialog.danger ? 'danger' : ''}`}
                onClick={() => close(dialog.mode === 'confirm' ? true : (dialog.value || ''))}
              >
                {dialog.confirmText}
              </button>
            </div>
          </div>
        </div>
      )}
    </Ctx.Provider>
  )
}
