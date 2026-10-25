import tempfile
import time
import unittest
from support import ServerProcess


class PublicationApiTests(unittest.TestCase):
    def test_simulator_ambiguity_query_idempotency_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            server=ServerProcess(directory).start()
            try:
                _,run=server.request('/v1/runs',{'title':'targets'},'create');run=server.wait(run['run_id'],{'SUCCEEDED'})
                path='/v1/runs/'+run['run_id']+'/publications'
                code,run=server.request(path,{'platform':'simulator','account':'demo','expected_version':run['state_version']},'intent')
                self.assertEqual(code,200);intent=run['publications'][0]['id'];version=run['state_version']
                body={'expected_version':version,'scenario':'lost_response'}
                code,dispatched=server.request(path+'/'+intent+'/dispatch',body,'dispatch');self.assertEqual(code,202)
                end=time.monotonic()+5
                while time.monotonic()<end:
                    run=server.request('/v1/runs/'+run['run_id'])[1]
                    if run['publications'][0]['state']=='UNKNOWN':break
                    time.sleep(.02)
                self.assertEqual(run['publications'][0]['state'],'UNKNOWN')
                self.assertEqual(server.request(path+'/'+intent+'/dispatch',body,'dispatch')[1],run)
                self.assertEqual(server.request(path+'/'+intent+'/dispatch',dict(body,scenario='success'),'dispatch')[0],409)
                self.assertEqual(server.request(path+'/'+intent+'/dispatch',{'expected_version':run['state_version']},'new')[0],409)
                code,final=server.request(path+'/'+intent+'/verify',{'expected_version':run['state_version']},'query')
                self.assertEqual(code,200);self.assertEqual(final['publications'][0]['state'],'PUBLISHED')
                self.assertTrue(final['publications'][0]['receipts'][-1]['work_id'].startswith('sim-'))
                server.stop();server.start()
                self.assertEqual(server.request('/v1/runs/'+run['run_id'])[1],final)
                self.assertEqual(server.request(path,{'platform':'real','account':'demo','expected_version':final['state_version']},'real')[0],422)
            finally:server.stop()
