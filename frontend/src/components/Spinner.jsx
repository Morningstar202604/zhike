import React from 'react'

export function Spinner({ full = false, label = '' }) {
  return (
    <div
      className={`grid place-items-center ${full ? 'h-full w-full min-h-[60vh]' : 'w-10 h-10'}`}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div
        className="w-8 h-8 rounded-full border-[3px] border-brand-soft border-t-brand animate-spin"
        style={{ animationDuration: '0.8s' }}
        aria-hidden="true"
      />
      <span className={label ? 'mt-3 text-xs text-muted' : 'sr-only'}>
        {label || '加载中'}
      </span>
    </div>
  )
}

export function ErrorBanner({ message, onRetry }) {
  return (
    <div role="alert" className="flex items-center gap-3 bg-danger-soft text-[#c93034] text-xs rounded-md px-4 py-3">
      <span className="flex-1">{message}</span>
      {onRetry && (
        <button type="button" className="btn ghost sm" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  )
}
