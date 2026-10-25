"""Independent public-call doubles for the pinned social command contracts."""
import asyncio
from dataclasses import make_dataclass, field
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from loopcast import social_bridge
from loopcast.social import SocialAdapter, validate_probe, SAU_REVISION


class SocialContractTests(unittest.TestCase):
    def test_all_four_commands_map_frozen_fields_and_only_report_submission(self):
        contracts = {
            'douyin': ('DouyinVideoUploadRequest','upload_video', {'publish_date':0,'thumbnail_file':None,'headless':True,'debug':False,'declaration':None}),
            'xiaohongshu': ('XiaohongshuVideoUploadRequest','upload_xiaohongshu_video', {'publish_date':0,'thumbnail_file':None,'headless':True,'debug':False}),
            'youtube': ('YouTubeVideoUploadRequest','upload_youtube_video', {'thumbnail_file':None,'visibility':'public','headless':True,'debug':False}),
            'bilibili': ('BilibiliVideoUploadRequest','upload_bilibili_video', {'tid':1,'publish_date':0,'thumbnail_file':None}),
        }
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);credential=root/'private.json';credential.write_text('{}')
            for platform,(name,function,options) in contracts.items():
                captured=[];module=types.SimpleNamespace()
                required=[(k,object) for k in ('account_name','video_file','title','description','tags')]
                request_type=make_dataclass(name,required+[(k,object,field(default=v)) for k,v in options.items()],slots=True)
                async def invoke(request):
                    captured.append(request)
                    self.assertEqual(module.resolve_account_file(platform,'controlled'),credential)
                    return credential
                setattr(module,name,request_type);setattr(module,function,invoke)
                runtime=types.SimpleNamespace(ensure_biliup_binary=lambda:None)
                modules={'sau_commands.'+platform:module,'uploader.bilibili_uploader.runtime':runtime}
                value={'credential_file':str(credential),'video_file':str(root/'video.mp4'),'cover_file':str(root/'cover.png'),
                       'title':'Frozen title','description':'Frozen description','fields':{'category':17,'visibility':'private' if platform=='youtube' else 'public','declaration':'AI content'}}
                with patch.object(social_bridge.importlib,'import_module',side_effect=lambda k:modules[k]):
                    result=asyncio.run(social_bridge.upload(platform,value,{'biliup':'/pinned/tool'}))
                self.assertEqual(result,{'outcome':'SUBMITTED'});self.assertEqual(len(captured),1)
                request=captured[0];self.assertEqual(request.video_file,root/'video.mp4');self.assertEqual(request.thumbnail_file,root/'cover.png');self.assertEqual(request.title,'Frozen title')
                if platform=='douyin': self.assertEqual(request.declaration,'AI content')
                if platform=='youtube': self.assertEqual(request.visibility,'private')
                if platform=='bilibili': self.assertEqual(request.tid,17);self.assertEqual(runtime.ensure_biliup_binary(),Path('/pinned/tool'))

    def test_actual_auth_entry_signatures_and_verified_identity_are_required(self):
        async def test():
            with tempfile.TemporaryDirectory() as directory:
                for platform in ('douyin','xiaohongshu','youtube'):
                    seen=[]
                    async def setup(account_file,handle=False,return_detail=False,headless=False):
                        seen.append((account_file,handle,return_detail,headless));Path(account_file).write_text('{}');return {'success':True}
                    module=types.SimpleNamespace(**{platform+'_setup':setup})
                    async def verify(p,credential,settings):
                        self.assertEqual(p,platform);self.assertTrue(credential.is_file());return {'stable_subject':'server-verified','label':'Label'}
                    with patch.object(social_bridge.importlib,'import_module',return_value=module),patch.object(social_bridge,'verified_profile',side_effect=verify):
                        value=await social_bridge.authorize(platform,Path(directory),{})
                    self.assertEqual(value['profile']['stable_subject'],'server-verified');self.assertEqual(seen[0][1:],(True,True,True))
        asyncio.run(test())

    def test_live_capabilities_never_invent_query_or_scheduling_and_default_off(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter=SocialAdapter(directory)
            self.assertFalse(adapter.sending_enabled);self.assertEqual(adapter.mode,'live')
            for capability in adapter.capabilities():
                self.assertFalse(capability['configured']);self.assertFalse(capability['query']);self.assertFalse(capability['schedule'])
            self.assertEqual(len(SAU_REVISION),40)

    def test_identity_probe_only_own_origin_and_stable_subject_paths(self):
        probe={'url':'https://creator.douyin.com/own/profile','id_path':['data','uid'],'label_path':['data','nickname'],'success_path':['code'],'success_value':0}
        self.assertEqual(validate_probe('douyin',probe),probe)
        for changed in ({'url':'https://example.org/profile'},{'url':'http://creator.douyin.com/profile'}, {'id_path':['data','nickname']}, {'url':'https://creator.douyin.com/profile?cookie=secret'}):
            with self.assertRaises(ValueError): validate_probe('douyin',probe|changed)
