# 群凭据发现工具接线

状态：工具切片冻结；根代理持有存储/helper/HTTP与卡片权限实现，统一严格gate与真实平台验收尚待进行。

## 单一权威

继续使用原 `request_credentials`，增加 `operation=list|find`。列表与查询仅委托 `task_credential_groups.discover_group_credentials`，不扫描私密目录、不解密、不复制授权，不排序或覆盖宿主选择。工具返回原 helper 的安全 `credential_discovery`，包含候选摘要、selected_credential_ref、selection_reason、own_credential_present。

`find` 需要准确 URL，可选 credential_name 只用于筛选；origin、当前 owner/群成员/HostActor/原任务和共享资格全部在宿主检查。自然名称、工具检索关键词和模型描述不授予权限。现有工具权限与子代理角色上界不扩大。

## 查找、使用和新卡

实际填写人同站点账号优先；没有本人账号时由宿主按本群固定顺序选可用引用。本人401/403、过期或其它失败不能静默切他人高权限账号；用户明确撤销自己的引用后，才可重新find使用本群其它账号。工具不自行实现选择或失败回退。

模型软提示优先find已保存引用，选中后直接交原configure/web_fetch；只有确实缺失或用户明确新增/换账号才申请新卡。`operation=request` 始终明确发新卡，不能因为别人已有账号阻止本人填写受限账号，也不隐式复用。原输入等待、取消、刷新和运输确认不变。

新卡 `access_scope` 可省略交原宿主按来源决定，也可显式task；个人不能选择group。卡片与持久共享/管理权限由根代理实现，本工具不从群展示方式猜授权。`operation=share` 委托原 `task_credential_forms.share_credential`，只要求准确原引用、沿原mutating权限；不新增人工审批。只有原请求人或实际填写人当前成员可原位升级，密文与credential_ref不变、签名重签，不重填、不复制秘密、不再发卡。

## 当前验证

接口先红15项中11失败，原XML保留。最终新文件33项通过，0失败/错误/跳过；含schema/只读effect、受限tool_search、真实Registry、fake native LLM三轮查询、真实同群C新请求find→HTTP使用A、B即便直传Aref仍执行本人B（401/403均不自动换号）、正式revoke→find选A。真实旧task卡的shareable摘要仅本人管理者可见、C不可见；原share原位重签后C新请求HTTP成功。新request默认group/显式group/task继续waiting_input，个人group请求与跨群/私聊share拒绝。模型、末端HTTP与官方成员/卡运输为合成替身，其余原Store/TaskLink/claim/typedActor、签名回调、加密、HTTP凭据handler均实际运行。

扩大到旧五文件79项的开发态组合有76通过/3失败：两个原preflight群case没有完整成员fixture，被新默认共享的前置成员核验拒绝；一个原self-context群配置case缺typedActor/授权，在精确HEAD f1961cf00隔离源码archive同节点复现。已交根处理、未删节点/跳过/放宽产品门；不能将该组合记作全绿。准确XML/SHA和文件清单见仓外group-credential-tool-frozen-handoff.json。

未读真实凭据、未访问平台、未修改服务或部署。

建议下一步：根收口旧fixture与存储/HTTP冻结后，保留完整清单统一focused和严格gate，再新发行与少量真实群任务验收。只读独立审阅可并行，不能并行修改同一权限文件；生产平台验收由根代理独占。
