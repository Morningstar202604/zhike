/**
 * 品牌标识组件。
 *
 * 设计说明：
 * - 单一渐变方块 + 对话气泡轮廓：既表达「沟通」也表达「服务」
 * - 气泡内的三个笔画抽象自「知」字，远看是文字，近看是对话线条
 * - 尺寸通过 size 控制，颜色随主题（浅色底用深色字，深色底用白字）
 */
import { memo } from 'react'

interface LogoProps {
  size?: number
  /** 是否在深色背景上（侧栏） */
  onDark?: boolean
  /** 是否显示文字 */
  withText?: boolean
  /** 副标题 */
  subtitle?: string
}

export const Logo = memo(function Logo({ size = 32, onDark = false, withText = false, subtitle }: LogoProps) {
  const gradId = onDark ? 'zkg-dark' : 'zkg-light'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: size * 0.34 }}>
      <svg
        width={size}
        height={size}
        viewBox="0 0 64 64"
        fill="none"
        role="img"
        aria-label="知客 Zhike"
        style={{ flexShrink: 0 }}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={onDark ? '#5B7BFF' : '#4F6BFF'} />
            <stop offset="1" stopColor={onDark ? '#8B6BFF' : '#7C5CFF'} />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="16" fill={`url(#${gradId})`} />
        <path
          d="M18 22.5c0-2.5 2-4.5 4.5-4.5h19c2.5 0 4.5 2 4.5 4.5v13c0 2.5-2 4.5-4.5 4.5H31l-8 6v-6h-.5c-2.5 0-4.5-2-4.5-4.5v-13z"
          fill="#fff"
          fillOpacity={onDark ? 1 : 0.97}
        />
        <circle cx="26" cy="27" r="2.1" fill={`url(#${gradId})`} />
        <path
          d="M33 25.5h8M33 30h8M27 33.5h14"
          stroke={`url(#${gradId})`}
          strokeWidth="2.2"
          strokeLinecap="round"
        />
      </svg>
      {withText && (
        <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
          <span
            style={{
              fontSize: size * 0.44,
              fontWeight: 650,
              letterSpacing: '0.2px',
              color: onDark ? '#E7E9EC' : 'var(--ink)',
            }}
          >
            知客 <span style={{ fontWeight: 500, opacity: 0.75 }}>Zhike</span>
          </span>
          {subtitle && (
            <span style={{ fontSize: size * 0.31, color: onDark ? '#6D737C' : 'var(--muted)', marginTop: 1 }}>
              {subtitle}
            </span>
          )}
        </span>
      )}
    </span>
  )
})

export default Logo
