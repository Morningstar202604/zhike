import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface AppStore {
  sessionId: string | null
  visitorId: string | null
  /** 访客身份令牌：首次声明 visitor_id 时由服务端下发，复用该 id 必须出示 */
  visitorToken: string | null
  opId: string | null
  adminToken: string
  /** 会话归属凭据：sid -> session_secret。web 访客会话必须带 X-Session-Secret 才能读写。 */
  sessionSecrets: Record<string, string>
  // 草稿（移动端切页不丢输入）
  draft: Record<string, string>
  setSession: (id: string | null) => void
  setVisitorId: (id: string | null) => void
  setVisitorToken: (t: string | null) => void
  setOpId: (id: string | null) => void
  setAdminToken: (t: string) => void
  setSessionSecret: (sid: string, secret: string) => void
  getSessionSecret: (sid: string) => string | undefined
  setDraft: (page: string, value: string) => void
  resetDraft: (page: string) => void
}

const rand = () => Math.random().toString(36).slice(2, 10)

export const useAppStore = create<AppStore>()(
  persist(
    (set, get) => ({
      sessionId: null,
      visitorId: null,
      visitorToken: null,
      opId: null,
      adminToken: '',
      sessionSecrets: {},
      draft: {},
      setSession: (id: string | null) => set({ sessionId: id }),
      setVisitorId: (id: string | null) => set({ visitorId: id }),
      setVisitorToken: (t: string | null) => set({ visitorToken: t }),
      setOpId: (id: string | null) => set({ opId: id }),
      setAdminToken: (t: string) => set({ adminToken: t }),
      setSessionSecret: (sid: string, secret: string) =>
        set((s: AppStore) => ({ sessionSecrets: { ...s.sessionSecrets, [sid]: secret } })),
      getSessionSecret: (sid: string) => get().sessionSecrets[sid],
      setDraft: (page: string, value: string) => set((s: AppStore) => ({ draft: { ...s.draft, [page]: value } })),
      resetDraft: (page: string) => set((s: AppStore) => ({ draft: { ...s.draft, [page]: '' } })),
    }),
    {
      name: 'cs-agent-app',
      partialize: (s: AppStore) => ({
        visitorId: s.visitorId,
        visitorToken: s.visitorToken,
        opId: s.opId,
        adminToken: s.adminToken,
        sessionSecrets: s.sessionSecrets,
        draft: s.draft,
      }),
    },
  ),
)

export function ensureVisitorId() {
  let v = useAppStore.getState().visitorId
  if (!v) {
    v = 'v-' + rand()
    useAppStore.getState().setVisitorId(v)
  }
  return v
}

export function ensureOpId() {
  let v = useAppStore.getState().opId
  if (!v) {
    v = 'op-' + rand()
    useAppStore.getState().setOpId(v)
  }
  return v
}
