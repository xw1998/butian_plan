from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont
root=Path(__file__).resolve().parents[1]/'docs/论文/experiment_results/dream-effect-20261015'
data=json.loads((root/'results.json').read_text(encoding='utf-8'))['summary']
im=Image.new('RGB',(1500,950),'white'); d=ImageDraw.Draw(im)
f=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',30)
small=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',26)
colors=['#333333','#888888','#BBBBBB']
for j,(metric,label) in enumerate([('correct','Correct tasks'),('source_reads','Later source reads'),('verification_reads','Verification reads')]):
    y=100+j*250; d.text((50,y),label,font=f,fill='black')
    for i,(key,name) in enumerate([('off','Off'),('unverified','Unverified'),('verified','Verified')]):
        x=450+i*330; val=data[key][metric]
        d.rectangle((x,y+130-val*.35,x+130,y+130),fill=colors[i])
        d.text((x,y+145),name,font=small,fill='black'); d.text((x,y+85-val*.35),str(val),font=small,fill='black')
im.save(root/'effects.png',dpi=(300,300))
print(root/'effects.png')
