import ollama
from config import ROSTER

def route_model(user_input: str) -> str:
    """Smart router to pick the right brain for the job."""
    p = user_input.lower()
    
    if any(k in p for k in ['git', 'repo', 'architecture', 'complex code', 'hackathon project']):
        return 'advanced_coder'
    elif any(k in p for k in ['code', 'script', 'c++', 'arduino', 'debug', 'html']):
        return 'basic_coder'
    elif any(k in p for k in ['math', 'calculate', 'equation', 'formula']):
        return 'math'
    elif any(k in p for k in ['med', 'doctor', 'health', 'anatomy', 'biology']):
        return 'med'
    elif any(k in p for k in ['story', 'write', 'narrative', 'creative', 'character']):
        return 'creative'
    elif any(k in p for k in ['quick', 'fast', 'short answer']):
        return 'fast'
    else:
        return 'base'

def call_ollama(prompt: str, system_prompt: str, model_key: str) -> str:
    """
    Executes the model using a standard, full-text generation.
    REQUIRED for Text-to-Speech (TTS) so the voice engine can read the whole sentence at once.
    """
    actual_model_name = ROSTER.get(model_key, ROSTER['base'])
    
    try:
        response = ollama.chat(
            model=actual_model_name,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': prompt}
            ],
            options={
                "num_ctx": 4096,
                "temperature": 0.4
            }
        )
        return response['message']['content']
    except Exception as e:
        return f"Error connecting to local engine. Details: {e}"