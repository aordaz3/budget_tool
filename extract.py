from PIL import Image
import pytesseract

try:
    image = Image.open('/Users/aordaz3/Downloads/AleJandro & Karina.png')

    text = pytesseract.image_to_string(image)

    
except Exception as e:
    print(f"Error: {e}")