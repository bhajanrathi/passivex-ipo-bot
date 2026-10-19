import os, requests

token = os.environ["WHAPI_TOKEN"]
offset = 0
while True:
    r = requests.get("https://gate.whapi.cloud/groups",
                     params={"count": 100, "offset": offset},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    for g in r.get("groups", []):
        name = g.get("name") or ""
        if "ipo" in name.lower() or "listing" in name.lower() or "reminder" in name.lower():
            print(g["id"], "|", name, "|", g.get("participants_count"), "members")
    offset += 100
    if offset >= r.get("total", 0):
        break