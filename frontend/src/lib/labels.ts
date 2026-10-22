/**
 * 后端内部枚举 → 中文展示。
 *
 * 后端字段是稳定的机器标识（knowledge_miss 等），前端负责翻译。
 * 好处：后端不用为展示改语义，前端也能在缺失时优雅降级。
 */

export const ESCALATE_REASON: Record<string, string> = {
  knowledge_miss: '知识库未命中',
  feedback_not_helpful: '访客反馈没帮助',
  visitor_requested: '访客主动要求',
  low_confidence: '模型置信度不足',
  output_guardrail: '输出被安全护栏拦截',
  image_needs_human: '图片需人工查看',
  pipeline_error: '系统处理异常',
  agent_pending: '等待人工确认',
};

export const SESSION_STATUS: Record<string, string> = {
  ongoing: '进行中',
  pending_agent: '待接管',
  agent_handled: '坐席处理中',
  closed: '已关闭',
};

export const ACTION_STATUS: Record<string, string> = {
  created: '已创建',
  pending_approval: '待审批',
  approved: '已批准',
  executed: '已执行',
  denied: '已拒绝',
  failed: '失败',
  expired: '已过期',
  consumed: '已使用',
  blocked: '已拦截',
};

export const RUN_STATUS: Record<string, string> = {
  running: '运行中',
  done: '已完成',
  max_steps: '步数用尽',
  budget_exceeded: '超出预算',
  cancelled: '已取消',
  error: '出错',
  pending_approval: '等待审批',
};

export const CHANNEL_LABEL: Record<string, string> = {
  webchat: '网页',
  qq_group: 'QQ 群',
  qq_private: 'QQ 私聊',
  wecom: '企业微信',
  mp: '公众号',
  telegram: 'Telegram',
};

export const ACTION_LABEL: Record<string, string> = {
  // QQ / OneBot v11 动作
  get_login_info: '查询账号信息',
  get_status: '查询运行状态',
  get_version_info: '查询协议端版本',
  get_group_list: '获取群列表',
  get_group_info: '获取群信息',
  get_group_member_list: '获取群成员',
  get_group_member_info: '获取成员信息',
  get_friend_list: '获取好友列表',
  get_stranger_info: '获取陌生人信息',
  get_msg: '获取消息',
  get_forward_msg: '获取合并转发',
  get_group_msg_history: '获取群历史消息',
  get_group_file_list: '获取群文件列表',
  get_group_file_url: '获取群文件链接',
  send_msg: '发送消息',
  send_group_msg: '发送群消息',
  send_private_msg: '发送私聊消息',
  delete_msg: '撤回消息',
  set_group_ban: '群禁言',
  set_group_whole_ban: '全员禁言',
  set_group_card: '修改群名片',
  set_group_name: '修改群名称',
  send_group_notice: '发布群公告',
  upload_group_file: '上传群文件',
  upload_private_file: '上传私聊文件',
  set_friend_add_request: '处理好友请求',
  set_group_add_request: '处理入群请求',
  set_group_kick: '移出群成员',
  set_group_leave: '退出群聊',
  delete_friend: '删除好友',
  set_group_admin: '设置群管理员',
  set_group_special_title: '设置专属头衔',
  set_restart: '重启协议端',
  // 电脑动作
  'fs.read': '读取文件',
  'fs.write': '写入文件',
  'fs.list': '列出目录',
  'fs.stat': '文件信息',
  'fs.mkdir': '新建目录',
  'fs.move': '移动/改名',
  'fs.delete': '删除文件',
  'shell.exec': '执行命令',
  'proc.list': '查看进程',
  'proc.kill': '结束进程',
};

export const TOOL_LABEL: Record<string, string> = {
  query_order: '查询订单',
  initiate_refund: '申请退款',
  lookup_policy: '查询政策',
  // 以下是内部流程标记，出现在统计里时也要能读懂
  feedback: '访客差评升级',
  injection_guard: '注入拦截',
  vision: '图片识别',
};

export const RISK_LABEL: Record<number, string> = {
  0: '只读',
  1: '可逆写',
  2: '破坏性',
};

/** 通用查询：未知值原样返回（便于发现后端新增枚举） */
export function label(map: Record<string, string>, key?: string | null, fallback = '-'): string {
  if (!key) return fallback;
  return map[key] || key;
}
