# validators

Future cross-domain validation layer returning explicit results and error codes.

如果某个校验规则要被 gateway、subagent、tooling 多处复用，就放这里。
validator 只判断“合不合格”，不直接修复、不直接改状态。
