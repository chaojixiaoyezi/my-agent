# LLM: 此处只定义后台完成信封的共享版本/字段，不管理状态或把自然语言作为机器决策。
# 模块用途: 给会话装配与主模型收口提供同一份公开回执协议标识，避免反向导入执行模块。
CONTRACT_KEY = 'background_public_receipt'
RESULT_SCHEMA = 'background-result.v1'
RECEIPT_FAILURE_CODES = frozenset({
    'BACKGROUND_RECEIPT_CONTRACT_INVALID', 'BACKGROUND_RECEIPT_CONTRACT_DISABLED',
    'BACKGROUND_RECEIPT_PUBLIC_OBLIGATION', 'BACKGROUND_RECEIPT_PUBLIC_UNAVAILABLE',
    'MODEL_RESPONSE_PROVENANCE_MISSING', 'MODEL_RESPONSE_PROVENANCE_MISMATCH', 'MODEL_RESPONSE_PURPOSE_MISMATCH',
})


# LLM: 此失败使用已登记code，不赋予重新执行业务的权限；后台宿主沿原scheduler异常收口，内部产出仍在原账。
# 类用途: 将不可交付的完成合同错误交给已有后台失败结算，避免空失败回执被当作待外发而重跑业务。
class BackgroundPublicReceiptError(RuntimeError):
    # LLM: 只允许协议固定错误码，不能把模型正文或任意词转成机器权威。
    # 函数用途: 保存后台宿主消费的结构化失败原因，不携带内部执行正文。
    def __init__(self, error_code: str):
        if error_code not in RECEIPT_FAILURE_CODES:
            raise ValueError('未知后台完成合同错误码')
        self.error_code = error_code
        super().__init__(error_code)
