"""Quick smoke test for MimicWX core modules"""
from mimicwx.onebot import IdMap, _parse_cq, _msg_to_actions, _message_event, _lifecycle_event

# Test IdMap
m = IdMap()
nid = m.to_id("testuser")
assert m.to_name(nid) == "testuser"
assert m.resolve(nid) == "testuser"
assert m.resolve("direct_name") == "direct_name"
print(f"✅ IdMap OK: testuser -> {nid} -> {m.to_name(nid)}")

# Test CQ parser
segs = _parse_cq("你好[CQ:face,id=178]世界[CQ:image,file=abc.jpg]")
assert len(segs) == 4
assert segs[0] == {"type": "text", "data": {"text": "你好"}}
assert segs[1] == {"type": "face", "data": {"id": "178"}}
print(f"✅ CQ parse OK: {len(segs)} segments")

# Test msg_to_actions
actions = _msg_to_actions([
    {"type": "text", "data": {"text": "hello "}},
    {"type": "text", "data": {"text": "world"}},
    {"type": "image", "data": {"file": "test.png"}},
], m)
assert len(actions) == 2  # merged text + image
assert actions[0] == {"action": "text", "content": "hello world"}
assert actions[1] == {"action": "image", "content": "test.png"}
print(f"✅ msg_to_actions OK: {actions}")

# Test event construction
evt = _message_event("bot1", 1, 12345, "Alice", [{"type":"text","data":{"text":"hi"}}], "hi")
assert evt["post_type"] == "message"
assert evt["message_type"] == "private"
print(f"✅ message_event OK: post_type={evt['post_type']}")

# Test group event
evt2 = _message_event("bot1", 2, 12345, "Alice", [{"type":"text","data":{"text":"hi"}}], "hi", group_id=99999)
assert evt2["message_type"] == "group"
assert evt2["group_id"] == 99999
print(f"✅ group message_event OK")

lc = _lifecycle_event("bot1")
assert lc["meta_event_type"] == "lifecycle"
print(f"✅ lifecycle_event OK")

# Test config
from mimicwx.config import Config
c = Config()
assert c.ws_url == "ws://127.0.0.1:2536/OneBotv11"
assert c.self_id == "MimicWX"
c2 = Config.from_yaml("config.yaml")
print(f"✅ Config OK: ws_url={c2.ws_url}")

print("\n🎉 All tests passed!")
