"""只在自有临时目录诊断Windows热消息身份，不访问用户资料或模型。"""
import argparse,json,os,platform,sys,tempfile,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);args=p.parse_args()
sys.path.insert(0,str(Path(args.source).resolve()))
from agent_py_agent.agent.common.nofollow_fs import file_descriptor_change_ns, file_identity_snapshot
from agent_py_agent.agent.conversation.message_cursor_slice import read_after_slice,MessageReadAllowance,source_stamp_is_trustworthy
from agent_py_agent.agent.io.cursor_cache import file_stamp
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
fields=('st_dev','st_ino','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns','st_birthtime_ns')
def view(info):return {k:getattr(info,k,None) for k in fields}
result={'schema':'hot-slice-diagnostic.v1','python':sys.version,'platform':platform.system(),'samples':[]}
with tempfile.TemporaryDirectory(prefix='r8-win-hot-slice-') as directory:
 path=Path(directory)/'messages.jsonl'
 with path.open('wb') as stream:
  for i in range(300):stream.write((json.dumps({'message_id':f'diag-{i:04d}','thread_id':'diagnostic','role':'user','content':'synthetic','created_at':i,'metadata':{}})+'\n').encode())
 old=time.time()-10;os.utime(path,(old,old));time.sleep(2.05)
 with path.open('rb') as handle:
  before=os.fstat(handle.fileno());changed=file_descriptor_change_ns(handle.fileno(),before)
  path_before=path.stat()
  result['initial']={'fd':view(before),'path':view(path_before),'native_changed_ns':changed,'trusted':source_stamp_is_trustworthy(file_stamp(before),changed),'raw_path_fd_different_fields':[k for k in fields if getattr(before,k,None)!=getattr(path_before,k,None)]}
  try:
   checked,checked_change=file_identity_snapshot(path)
   result['canonical_path_snapshot']={'fd':view(checked),'native_changed_ns':checked_change,'same_read_fd_stamp':file_stamp(checked)==file_stamp(before),'same_read_fd_native_change':checked_change==changed}
  except Exception as error:
   result['canonical_path_snapshot']={'exception_type':type(error).__name__}
 for _ in range(5):
  budget=RetentionScanBudget.default()
  try:
   outcome=read_after_slice(path,'diag-0149',2,MessageReadAllowance(budget))
   result['samples'].append({'reason':outcome.reason,'partial':outcome.partial,'has_more':outcome.has_more,'rows':len(outcome.rows),'errors':[e.get('error_type')for e in outcome.load_errors],'entries':budget.entries,'bytes':budget.size,'continuation':None if outcome.continuation is None else {'phase':outcome.continuation.phase,'offset':outcome.continuation.offset,'changed_ns':outcome.continuation.changed_ns}})
  except Exception as error:
   result['samples'].append({'exception_type':type(error).__name__});break
 with path.open('rb') as handle:
  final=os.fstat(handle.fileno());result['final']={'fd':view(final),'path':view(path.stat()),'native_changed_ns':file_descriptor_change_ns(handle.fileno(),final)}
output=Path(args.output);output.write_text(json.dumps(result,indent=2),encoding='utf-8');os.chmod(output,0o600)
print(json.dumps(result))
