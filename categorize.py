import os
from google import genai
from google.genai import types

# 1. Initialize the client properly
client = genai.Client()

# 2. Use generate_content instead of interactions.create
response = client.models.generate_content(
    model="gemini-3.5-flash",  # Using the current standard Flash tier model
    contents="Categorize the following customer review: 'The app crashes every time I try to upload a photo.'",
    config=types.GenerateContentConfig(
        system_instruction="You are a strict data categorizer. Output only one label from this list: [Bug, Feature Request, Question, Compliment]. Do not include any other text."
    )
)

# 3. Print the text result
print(response.text)
