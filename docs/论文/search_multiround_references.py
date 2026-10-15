from pathlib import Path
import asyncio, sys
sys.path.insert(0, r'C:/Users/14905/AppData/Roaming/WPS 灵犀/serverdir/official_skills/paper-writer/scripts')
import academic_search as ac
async def main():
    await ac.multi_round_search(rounds=[
        {'keywords':['LongMemEval Benchmarking Chat Assistants on Long-Term Interactive Memory','Reflexion Language Agents with Verbal Reinforcement Learning','MemoryBank Enhancing Large Language Models with Long-Term Memory'], 'sources':['arxiv'], 'per_page':3},
        {'keywords':['Retrieval Augmented Generation for Knowledge Intensive NLP Tasks','Self Refine Iterative Refinement with Self Feedback','Voyager An Open Ended Embodied Agent with Large Language Models'], 'sources':['arxiv'], 'per_page':3}],
        topic='Longitudinal provenance and version aware agent memory experiments', target=18, per_page=3,
        refs_json=str(Path(__file__).parent/'references/multiround-references.json'))
asyncio.run(main())
