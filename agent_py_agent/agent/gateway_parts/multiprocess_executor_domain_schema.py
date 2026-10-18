# LLM: 只校验P1 v2原执行域结构，实际namespace/source/Popen及整树退出由C内核适配验证；不默迁移v1或用磁盘标记重建权限，同步records/P1/P6 tests。
# 模块用途: 为原P1增加显式Linux namespace事实格式，保持旧v1字节不变。


# LLM: namespace身份是dev/ino各自严格整数；纯记录不证明引用仍活，调用方必须持原内核引用而非再open数字路径。
# 函数用途: 校验原namespace或C父namespace的固定格式。
def _namespace(value):
    from .multiprocess_records import MultiprocessRecordError

    if (type(value) is not dict or set(value) != {'device', 'inode'}
            or type(value['device']) is not int or value['device'] < 0
            or type(value['inode']) is not int or value['inode'] < 1):
        raise MultiprocessRecordError('invalid_namespace_identity', 'execution_domain')


# LLM: init逐字匹配同P1原identity；creator须先是原process_identity版本再核hostPPID，不能用另一种合法记录绕到缺字段异常。
#   C/W角色/原分区显式；bool/缺字段/相同域/陌生实例拒绝，实际活source而非JSON决定权。
# 函数用途: 校验登记前的Linux执行域结构和精确创建者记录类型，不读取内核或签发执行权。
def validate_execution_domain(value, identity):
    from .multiprocess_records import MultiprocessRecordError, _expect_schema, validate_record

    fields = {'schema_version', 'platform', 'method', 'namespace', 'parent_namespace', 'init_identity',
              'host_parent_pid', 'init_namespace_pid', 'creator_identity', 'source_role', 'source_worker_index'}
    if type(value) is not dict or set(value) != fields:
        raise MultiprocessRecordError('invalid_execution_domain_fields', 'execution_domain')
    if (value['schema_version'] != 'multiprocess_execution_domain.v1' or value['platform'] != 'linux'
            or value['method'] != 'pid_namespace'):
        raise MultiprocessRecordError('invalid_execution_domain_method', 'execution_domain')
    _namespace(value['namespace'])
    _namespace(value['parent_namespace'])
    _expect_schema(value['creator_identity'], 'multiprocess_process_identity.v1')
    if (value['namespace'] == value['parent_namespace'] or type(value['host_parent_pid']) is not int
            or value['host_parent_pid'] < 1 or value['host_parent_pid'] == identity['pid']
            or type(value['init_namespace_pid']) is not int or value['init_namespace_pid'] != 1
            or validate_record(value['init_identity']) != identity
            or validate_record(value['creator_identity'])['pid'] != value['host_parent_pid']
            or type(value['source_role']) is not str or value['source_role'] not in {'C', 'W'}
            or value['creator_identity']['host_id'] != identity['host_id']
            or type(value['source_worker_index']) is not int or value['source_worker_index'] < 0):
        raise MultiprocessRecordError('invalid_execution_domain_binding', 'execution_domain')
