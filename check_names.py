#!/usr/bin/env python3
import json
import os

cache_dir = os.path.expanduser('~/.fhir/packages/hl7.fhir.au.base#6.0.0/package')
count = 0
ncts_names = []

for entry in os.scandir(cache_dir):
    if entry.is_file() and entry.name.endswith('.json'):
        with open(entry.path, 'r') as f:
            try:
                data = json.load(f)
                if data.get('resourceType') == 'ValueSet':
                    url = data.get('url', '')
                    if 'healthterminologies.gov.au' in url:
                        name = data.get('name') or data.get('title', '')
                        ncts_names.append(name)
            except:
                pass

# Print all names first
print(f"Found {len(ncts_names)} NCTS valuesets\n")
print("All NCTS ValueSet names:")
for name in sorted(ncts_names):
    print(f"  {name}")

# Now search for our target keywords
print("\n\nSearching for target keywords:")
keywords = ['state', 'territory', 'specialty', 'organisation', 'role', 
            'ingredient', 'procedure', 'site', 'condition', 'medication', 'adverse']

for keyword in keywords:
    matches = [n for n in ncts_names if keyword.lower() in n.lower()]
    if matches:
        print(f"\n'{keyword}' matches:")
        for match in matches:
            print(f"  - {match}")
