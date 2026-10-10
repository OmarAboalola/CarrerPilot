"""Bound model context to the question, with explicit counts and omitted-source coverage."""
import json
import re
from datetime import UTC, datetime, timedelta


def compact_context(data, question, previous_question="", previous_source_ids=None):
    previous_source_ids = previous_source_ids or []
    now=datetime.now(UTC)
    week_start=(now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    weekly=[]
    categories={}
    for item in data.get("emails",[]):
        try:
            received=datetime.fromisoformat(item.get("received_at") or "")
            if received.tzinfo and week_start <= received <= now:
                weekly.append(item)
                kind=item.get("category") or "unknown"
                categories[kind]=categories.get(kind,0)+1
        except ValueError:
            continue
    context = {"question":question, "now":datetime.now(UTC).isoformat(),
               "profile_preferences":data.get("profile_preferences",{}),
               "previous_question":previous_question,"previous_source_ids":previous_source_ids,
               "coverage":data.get("coverage",{}),"retrieved_counts":{
                   "jobs":len(data.get("jobs",[])),"emails":len(data.get("emails",[])),
                   "response_required":sum(bool(e.get("requires_response")) for e in data.get("emails",[])),
                   "this_week_utc":{"start":week_start.isoformat(),"total":len(weekly),"by_category":categories,"automated":sum(bool(e.get("automated")) for e in weekly)}},
               "market":{k:data.get("market",{}).get(k) for k in ["sample_size","learning","caveat"]},
               "emails":[],"jobs":[]}
    for item in data.get("emails",[]):
        context["emails"].append({k:item.get(k) for k in ["id","subject","category","requires_response",
                                "response_state","deadline","deadline_evidence","received_at","uncertainty","automated"]}
                                | {"summary":item.get("summary","")[:600],"evidence":item.get("evidence","")[:700]})
    context["emails"].sort(key=lambda e:(e["id"] not in previous_source_ids,bool(e.get("automated") and e.get("category")=="other"),not e.get("requires_response")))
    mail_only = re.search(r"recruit|mail|interview|assessment|deadline|reply|answered|conversation",question,re.I) and not re.search(r"first|priorit|urgent",question,re.I)
    for item in data.get("jobs",[])[:0 if mail_only else 6]:
        context["jobs"].append({k:item.get(k) for k in ["id","title","company","location","score","matches","gaps","proficiency_questions","blockers","unknowns","application_status"]}
                              | {"required_evidence":item.get("requirements",{}).get("required",[])[:6]})
    included_jobs=len(context["jobs"])
    context["context_coverage"]={"omitted_jobs":len(data.get("jobs",[]))-included_jobs,"omitted_emails":0}
    while len(json.dumps(context,ensure_ascii=False))>12000 and (context["emails"] or context["jobs"]):
        group="emails" if len(context["emails"])>3 else "jobs"
        if not context[group]:
            group="emails"
        context[group].pop()
        context["context_coverage"]["omitted_"+group]+=1
    return context
