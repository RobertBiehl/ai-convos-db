import copy, time

import pytest

from ai_convos_remote.control import approved, proposal, record, sign, state_hash, verify_state, vote
from ai_convos_remote.protocol import certificate, digest, identity


def person(name):
    root,device=identity(name+" root"),identity(name); return root,device,record(root["id"],root["sign_public"],device,certificate(root,root["id"],device))
def genesis(root,device,entry):
    body={"v":1,"kind":"workspace.state","workspace":"w","scope":"team","revision":1,"prev":None,"epoch":1,"key_commitment":digest(b"k1"),"members":{root["id"]:{"role":"admin","joined":1,"history_from":1,"selected":[]}},"devices":{device["id"]:entry},"removed":[],"action":"create","approval":None}; return sign(device,body)
def successor(device,base,action,devices=None,members=None,removed=None,approval=None,epoch=None):
    return sign(device,{"v":1,"kind":"workspace.state","workspace":"w","scope":"team","revision":base["revision"]+1,"prev":state_hash(base),"epoch":epoch if epoch is not None else base["epoch"]+1,"key_commitment":digest(f"k{base['epoch']+1}".encode()),"members":members or copy.deepcopy(base["members"]),"devices":devices or copy.deepcopy(base["devices"]),"removed":removed if removed is not None else list(base["removed"]),"action":action,"approval":approval,"approved_at":time.time()})


def test_self_approval_inherits_user_state_and_rejects_cross_user_or_removed():
    ar,ad,a=person("alice"); base=genesis(ar,ad,a); _,new,entry=person("alice-new"); entry={**entry,"user":ar["id"],"root_public":ar["sign_public"],"certificate":certificate(ar,ar["id"],new),"device":{k:new[k] for k in ("id","name","sign_public","box_public")}}
    req=proposal(new,"w",base,{**entry,"history":False},time.time()+60); state=successor(ad,base,"self_approve",{**base["devices"],new["id"]:entry},approval={"proposal":req,"votes":[]}); assert verify_state(state,base)
    br,bd,b=person("bob"); bad=successor(ad,base,"self_approve",{**base["devices"],bd["id"]:b},approval={"proposal":proposal(bd,"w",base,b,time.time()+60),"votes":[]})
    with pytest.raises(ValueError,match="workspace"): verify_state(bad,base)
    removed={**base,"removed":[new["id"]]}; removed["signature"]=sign(ad,{k:v for k,v in removed.items() if k not in ("author","signature")})["signature"]
    with pytest.raises(ValueError,match="workspace"): verify_state(successor(ad,removed,"self_approve",{**removed["devices"],new["id"]:entry},approval={"proposal":proposal(new,"w",removed,entry,time.time()+60),"votes":[]}),removed)


def test_majority_is_one_vote_per_user_and_bound_to_signed_base():
    ar,ad,a=person("alice"); br,bd,b=person("bob"); cr,cd,c=person("carol"); dr,dd,d=person("dora"); base=genesis(ar,ad,a); base["members"]|={br["id"]:{"role":"member","joined":1,"history_from":1,"selected":[]},cr["id"]:{"role":"member","joined":1,"history_from":1,"selected":[]},dr["id"]:{"role":"member","joined":1,"history_from":1,"selected":[]}}; base["devices"]|={bd["id"]:b,cd["id"]:c,dd["id"]:d}; base=sign(ad,{k:v for k,v in base.items() if k not in ("author","signature")})
    _,target,entry=person("alice-recovered"); entry={**entry,"user":ar["id"],"root_public":ar["sign_public"],"certificate":certificate(ar,ar["id"],target),"device":{k:target[k] for k in ("id","name","sign_public","box_public")},"history":False}; req=proposal(target,"w",base,entry,time.time()+60)
    votes=[vote(bd,br["id"],req),vote(cd,cr["id"],req)]; assert approved(base,req,votes)==(2,3)
    state=successor(bd,base,"quorum_approve",{**base["devices"],target["id"]:entry},approval={"proposal":req,"votes":votes}); assert verify_state(state,base)
    with pytest.raises(ValueError,match="requires"): approved(base,req,[votes[0]])
    with pytest.raises(ValueError,match="conflicting"): approved(base,req,[votes[0],vote(bd,br["id"],req,False),votes[1]])


def test_state_chain_rejects_role_change_split_transition_and_bad_commitment_shape():
    ar,ad,a=person("alice"); base=genesis(ar,ad,a); _,target,entry=person("alice-new"); entry={**entry,"user":ar["id"],"root_public":ar["sign_public"],"certificate":certificate(ar,ar["id"],target),"device":{k:target[k] for k in ("id","name","sign_public","box_public")}}; req=proposal(target,"w",base,entry,time.time()+60)
    changed=copy.deepcopy(base["members"]); changed[ar["id"]]["role"]="member"; state=successor(ad,base,"self_approve",{**base["devices"],target["id"]:entry},changed,approval={"proposal":req,"votes":[]})
    with pytest.raises(ValueError,match="policy"): verify_state(state,base)
    stale=successor(ad,base,"self_approve",{**base["devices"],target["id"]:entry},approval={"proposal":req,"votes":[]}); stale["prev"]="wrong"; stale=sign(ad,{k:v for k,v in stale.items() if k not in ("author","signature")})
    with pytest.raises(ValueError,match="chain"): verify_state(stale,base)
    with pytest.raises(ValueError,match="membership"): verify_state(successor(ad,base,"membership",{**base["devices"],target["id"]:entry}),base)
