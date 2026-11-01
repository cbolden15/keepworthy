"""Bounded image-reading pass; retain observations independently of summaries."""
import json
from pathlib import Path


def observe(images, cfg, root, run_model):
    prompt = Path(root,'prompts/observe-media.md').read_text()
    records = []
    for start in range(0,len(images),8):
        batch = images[start:start+8]
        raw = run_model(prompt,'Extract source text and visual observations from the attached media.',batch,cfg.model,root)
        stripped=raw.strip()
        if stripped.startswith('```json') and stripped.endswith('```'):
            stripped=stripped[7:-3].strip()
        result=json.loads(stripped)
        if not isinstance(result,dict) or set(result)!={'images'} or not isinstance(result['images'],list) or len(result['images'])!=len(batch):
            raise ValueError('Incomplete visual observations')
        for i, row in enumerate(result['images'],1):
            if (not isinstance(row,dict) or set(row)!={'index','text','observations'} or row['index']!=i
                    or not all(isinstance(row[k],str) and len(row[k])<=100_000 for k in ('text','observations'))):
                raise ValueError('Invalid visual observation')
            records.append({**row,'index':start+i})
    return records


def render(records):
    return '\n\n'.join(f"### Image/frame {r['index']}\n\nExtracted text:\n{r['text'] or '(No readable text)'}\n\nObservations:\n{r['observations']}" for r in records)
