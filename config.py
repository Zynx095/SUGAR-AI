"""
config.py
Handles configuration for your 100% local, offline AI architecture.
Zero APIs. Zero cloud dependency.
"""
import os

# Vector Database Configuration
CHROMA_DB_DIR = "./chroma_db"

ROSTER = {
    'advanced_coder': 'deepseek-coder:6.7b', 
    'basic_coder': 'codellama',              
    'math': 'openhermes',                    # <--- Removed the hyphen here!
    'med': 'wizard-vicuna-uncensored',       
    'creative': 'mistral',                   
    'fast': 'phi3',                          
    'base': 'llama3'                         
}