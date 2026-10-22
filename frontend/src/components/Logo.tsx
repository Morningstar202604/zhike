/**
 * 品牌标识：朱印 · 知客
 *
 * 设计取向不是"科技感渐变"，而是把中文语境里的「印信」搬进产品：
 * - 方形圆角印面，内嵌一圈细边，像盖章的边框
 * - 印面正中是「知」，用宋体字形（衬线）承接线装书的阅读气质
 * - 右下角一枚朱砂小点，是印章的落款位置，也是整套视觉里唯一的暖色
 *
 * 三个尺寸档位共用同一几何：16px 时只保留印面与「知」，不画内边框，
 * 避免小尺寸下糊成一团。
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
  const id = `zk-seal-${onDark ? 'd' : 'l'}`
  const face = onDark ? '#F2EFE9' : '#0E5C55'
  const glyph = onDark ? '#0E5C55' : '#F7F5F0'
  const seal = '#C2452D'
  const showInner = size >= 24
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
          <clipPath id={id}>
            <rect width="64" height="64" rx="15" />
          </clipPath>
        </defs>
        <g clipPath={`url(#${id})`}>
          <rect width="64" height="64" fill={face} />
          {showInner && (
            <rect
              x="3.2"
              y="3.2"
              width="57.6"
              height="57.6"
              rx="12.4"
              stroke={glyph}
              strokeOpacity={onDark ? 0.22 : 0.18}
              strokeWidth="1"
            />
          )}
          <text
            x="32"
            y="45.5"
            textAnchor="middle"
            fontFamily='Songti SC, STSong, SimSun, "Noto Serif SC", serif'
            fontSize="38"
            fontWeight="600"
            fill={glyph}
            letterSpacing="-1"
          >
            知
          </text>
          {/* 落款朱点：整套界面里唯一的暖色 */}
          <circle cx="49.5" cy="49.5" r="3.1" fill={seal} />
        </g>
      </svg>
      {withText && (
        <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
          <span
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: size * 0.46,
              fontWeight: 600,
              letterSpacing: '0.04em',
              color: onDark ? '#E9E6DF' : 'var(--ink)',
            }}
          >
            知客
          </span>
          {subtitle && (
            <span
              style={{
                fontSize: size * 0.28,
                color: onDark ? '#7C837F' : 'var(--muted)',
                marginTop: 2,
                letterSpacing: '0.02em',
              }}
            >
              {subtitle}
            </span>
          )}
        </span>
      )}
    </span>
  )
})

export default Logo
