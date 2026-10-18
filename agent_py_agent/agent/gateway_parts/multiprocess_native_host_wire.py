# LLM: Addresses locate original C-selected resources only; the kernel sender and captured source still grant authority.
# 模块用途: Preserve the native host address derivation and unchanged READY/CONNECTED byte protocol.
import hashlib
import sys

_READY = b'background-host-native-ready.v1'
_CONNECTED = b'background-host-coordinator-ready.v1'


# LLM: Derived from original C-selected P1 reservation only; address/body never grants permission.
# 函数用途: Locate independent host resources while preserving the exact five-field v1 init frame.
def host_addresses(root, partition, identifier):
    key = hashlib.sha256(f'{root}\0{partition.c_run_id}\0{partition.index}\0{identifier}'.encode()).hexdigest()
    prefix = 'com.myagent.host.' if sys.platform == 'darwin' else '@myagent.host.'
    return prefix + key + '.c', prefix + key + '.h'
