import sys,asyncio
from pathlib import Path
sys.path.insert(0,r'C:/Users/14905/AppData/Roaming/WPS 灵犀/serverdir/official_skills/paper-writer/scripts')
import academic_search as ac
async def main():
 await ac.multi_round_search(rounds=[{'keywords':['Scaling Laws for Neural Language Models','Are Emergent Abilities of Large Language Models a Mirage','TruthfulQA Measuring How Models Mimic Human Falsehoods'],'sources':['arxiv'],'per_page':3},{'keywords':['Training language models to follow instructions with human feedback','Direct Preference Optimization Your Language Model is Secretly a Reward Model','大语言模型 幻觉 持续学习 经验'],'sources':['metaso'],'per_page':3}],topic='语言模型规模隐含知识监督幻觉涌现与经验持续学习',target=65,per_page=3,refs_json=str(Path(__file__).parent/'references/references.json'),zh_ratio=.2)
asyncio.run(main())
