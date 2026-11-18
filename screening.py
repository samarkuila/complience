import os, json
KW = ["nbfc","non-banking financial","non banking financial","systemically important","master direction","kyc","know your customer",
      "digital lending","fair practices","credit information","sarfaesi","pmla","money laundering","scale based regulation","microfinance",
      "outsourcing","cyber","fraud","lending","co-lending","gold loan","npa","asset classification","vehicle loan","nach","e-mandate","aadhaar",
      "data protection","sanction","consolidated list","insurance","corporate agent","accounting standard","audit"]
def llm_screen(title, text):
    if not os.getenv("AZURE_OPENAI_ENDPOINT"): return None
    from openai import AzureOpenAI
    cl = AzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"], api_key=os.environ["AZURE_OPENAI_API_KEY"],
                     api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01"))
    r = cl.chat.completions.create(model=os.environ["AZURE_OPENAI_DEPLOYMENT"], response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "You support Compliance at an Indian NBFC. Given a regulatory item, reply JSON: "
                   '{"nbfc_related": true/false, "document_type": "...", "reason": "one sentence"}. When unsure, say true.'},
                  {"role": "user", "content": f"Title: {title}\n\nText: {text[:4000]}"}])
    return json.loads(r.choices[0].message.content)
def screen(title, text, extra):
    blob = (title + " " + text).lower()
    hits = [k for k in KW + [e.lower() for e in extra] if k in blob]
    score, flag, reason = len(hits), "potentially relevant" if hits else "not flagged", ("Keywords: " + ", ".join(hits[:8])) if hits else "No NBFC keywords found"
    try:
        r = llm_screen(title, text)
        if r:
            flag = "potentially relevant" if (r.get("nbfc_related") or hits) else "not flagged"
            reason = f"AI: {r.get('reason','')} ({r.get('document_type','')}) | {reason}"
    except Exception as e:
        reason += f" | AI screening failed: {e}"
    # Items are never hidden: 'not flagged' only lowers priority. Compliance makes the final call.
    return {"flag": flag, "score": score, "reason": reason}
