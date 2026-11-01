import base64
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import ingest, local_media, saved_media, social, state, visuals


def test_media_helper_receives_bytes_with_clean_environment(tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'video')
    def runner(args,**kwargs):
        assert args==['/usr/bin/sudo','-n',local_media.HELPER]
        assert kwargs['env']=={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
        payload=json.loads(kwargs['input'])
        assert base64.b64decode(payload['base64'])==b'video'
        assert str(source) not in kwargs['input']
        return SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'output':{'transcript':'spoken words','frames':[{'timestamp':1,'base64':base64.b64encode(b'\xff\xd8\xffjpeg').decode()}]}}))
    result=local_media.extract(source,tmp_path,runner)
    assert result['transcript']=='spoken words'
    assert result['images'][0].stat().st_mode&0o777==0o600


def test_failed_local_worker_has_no_paid_fallback(tmp_path):
    source=tmp_path/'v';source.write_bytes(b'v')
    with pytest.raises(RuntimeError,match='paid fallback is disabled'):
        local_media.extract(source,tmp_path,lambda *a,**k:SimpleNamespace(returncode=1))


def test_cdn_scope_and_shortcodes():
    assert saved_media.instagram_id('DcSMK-cuYfC')=='3968287752473642946'
    assert saved_media.cdn_url('https://scontent.example.fbcdn.net/file')
    for url in ('https://fbcdn.net.evil.example/file','http://scontent.fbcdn.net/file','https://127.0.0.1/file','https://user@fbcdn.net/file'):
        with pytest.raises(ValueError):saved_media.cdn_url(url)


def test_carousel_preserves_images_and_full_videos():
    media={'carousel_media':[{'image_versions2':{'candidates':[{'width':1080,'url':'image'}]}},
                             {'video_versions':[{'width':720,'height':1280,'url':'video'}]}]}
    assert saved_media._ig_assets(media)==[('image','image'),('video','video')]


def test_facebook_images_are_scoped_to_requested_story(monkeypatch):
    url='https://www.facebook.com/person/posts/pfbidSource'
    def photo(identity,uri):return {'__typename':'Photo','id':identity,'image':{'uri':uri}}
    docs={'stories':[{'__typename':'Story','permalink_url':url,'attachments':[photo('1','source-photo')]},
                     {'__typename':'Story','permalink_url':'https://www.facebook.com/other/posts/other','attachments':[photo('2','unrelated-photo')]}]}
    body='<script type="application/json">'+json.dumps(docs)+'</script>'
    monkeypatch.setattr(saved_media,'Client',lambda _:SimpleNamespace(request=lambda _:body))
    assert saved_media.metadata(url,'cookies')==[('image','source-photo')]


def test_metadata_missing_source_media_does_not_use_other_facebook_video(monkeypatch):
    doc={'id':'other','videoDeliveryResponseFragment':{'progressive_urls':[{'progressive_url':'other-video'}]}}
    body='<script type="application/json">'+json.dumps(doc)+'</script>'
    monkeypatch.setattr(saved_media,'Client',lambda _:SimpleNamespace(request=lambda _:body))
    with pytest.raises(ValueError,match='Matching Facebook video'):
        saved_media.metadata('https://www.facebook.com/watch/?v=source','cookies')


def test_visual_pass_batches_every_image_and_preserves_numbers(tmp_path):
    (tmp_path/'prompts').mkdir();(tmp_path/'prompts/observe-media.md').write_text('extract')
    calls=[]
    def run(prompt,payload,images,model,root):
        calls.append(len(images))
        return json.dumps({'images':[{'index':i+1,'text':'2 tbsp; $123.45','observations':'source screenshot'} for i in range(len(images))]})
    result=visuals.observe([Path(f'{i}.jpg') for i in range(19)],SimpleNamespace(model='sonnet'),tmp_path,run)
    assert calls==[8,8,3] and len(result)==19
    assert result[-1]['index']==19
    assert '2 tbsp; $123.45' in visuals.render(result)


def test_visual_pass_rejects_missing_attachment_observations(tmp_path):
    (tmp_path/'prompts').mkdir();(tmp_path/'prompts/observe-media.md').write_text('extract')
    with pytest.raises(ValueError,match='Incomplete'):
        visuals.observe([Path('a.jpg')],SimpleNamespace(model='sonnet'),tmp_path,lambda *a:'{"images":[]}')


def cfg(tmp_path):
    return SimpleNamespace(state_path=tmp_path/'state.json',cookies_path=None,free_media_enabled=True,
                           model='sonnet',finance_content=None,open_brain_saved_spool_dir=None,
                           vault_dir=tmp_path/'vault',content_root='saved-content',topics=['Other'],subtopics={})


def deps(tmp_path):
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'jpeg')
    def model(prompt,payload,images,*a):
        if images:return json.dumps({'images':[{'index':1,'text':'entry $10, target $15','observations':'slide'}]})
        assert 'entry $10, target $15' in payload
        return 'summary'
    return SimpleNamespace(fetch_saved_media=lambda *a:{'kind':'video','files':[tmp_path/'video.mp4'],'meta':{}},
         extract_local_media=lambda *a:{'transcript':'[0-1] Source speech','images':[frame], 'frame_timestamps':[1],
                                        'audio_present':True,'duration':10,'model':'whisper'},
         run_claude_social=model,parse_model_output=lambda raw:({'topic':'Other'},'Body'),
         build_social_note=lambda *a:'Note\n'+str(a[3]))


def test_free_video_is_transcribed_and_visuals_retained_without_gemini(tmp_path,monkeypatch):
    config=cfg(tmp_path);d=deps(tmp_path)
    (tmp_path/'prompts').mkdir();(tmp_path/'prompts/observe-media.md').write_text('extract')
    item={'url':'https://www.instagram.com/p/code/','capture_source':'saved_feed',
          'saved_content_kind':'video','transcription_policy':'free_only','saved_caption':'caption'}
    key=ingest.url_key(item['url']);written=[]
    monkeypatch.setattr(social,'_write_social_note',lambda cfg,meta,key,content,*a:written.append(content))
    assert social.process_social_item(config,tmp_path,item,'date',d,key,'summary prompt')=='summarized'
    assert 'entry $10, target $15' in written[0] and 'Source speech' in written[0]
    assert key in state.load_state(config.state_path)['media_processed']
    assert social.process_social_item(config,tmp_path,item,'date',d,key,'prompt')=='duplicate'


def test_refresh_reuses_existing_note_and_dedupes_completed_repair(tmp_path):
    config=cfg(tmp_path)
    existing=config.vault_dir/'saved-content/Other/old.md';existing.parent.mkdir(parents=True)
    url='https://www.threads.com/@author/post/code'
    existing.write_text(f'---\nurl: "{url}"\n---\nOld caption summary')
    meta={'url':url,'platform':'threads','_media_refresh':True}
    result=social._write_social_note(config,meta,'key','Updated images','Other',processed='date')
    assert result==existing and existing.read_text()=='Updated images'
    assert existing.stat().st_mode&0o777==0o600
    assert len(list(config.vault_dir.rglob('*.md')))==1


def test_free_note_is_private_even_when_manual_runner_umask_is_permissive(tmp_path):
    config=cfg(tmp_path)
    previous=os.umask(0o002)
    try:
        meta={'url':'https://www.instagram.com/p/abc/','platform':'instagram','_private_media':True}
        header=f'---\nurl: "{meta["url"]}"\n---\n'
        first=social._write_social_note(config,meta,'key',header+'source observations','Other',processed='2026-10-31')
        assert first.stat().st_mode&0o777==0o600
        second=social._write_social_note(config,meta,'key',header+'updated observations','Other',processed='2026-11-01')
        assert first==second and first.read_text()==header+'updated observations'
    finally:
        os.umask(previous)


def test_batch_limit_requeues_unattempted_items(tmp_path):
    config=cfg(tmp_path);config.social_batch_size=1;config.free_media_enabled=False
    config.queue_path=tmp_path/'queue';config.social_prompt_path=Path('prompt')
    (tmp_path/'prompt').write_text('prompt')
    for i in range(3):ingest.append_item(config.queue_path,{'url':f'https://www.instagram.com/p/{i}/','saved_content_kind':'video','transcription_policy':'free_only'})
    result=social.drain_social_queue(config,tmp_path,'date',SimpleNamespace())
    assert len(result)==1
    assert len(config.queue_path.read_text().splitlines())==3
    assert not config.state_path.exists()


def test_media_namespace_never_shares_network_or_private_mounts(tmp_path):
    spec=importlib.util.spec_from_file_location('media_sandbox',Path(__file__).parents[1]/'bin/media_sandbox.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    cmd=module.command(tmp_path,tmp_path)
    assert '--unshare-all' in cmd and '--share-net' not in cmd
    assert '--cap-drop' in cmd and 'ALL' in cmd
    assert not any('/home/example' in s or '/run/' in s for s in cmd)
    assert '/runtime' in cmd and '/model' in cmd
