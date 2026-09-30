with open('templates/index.html', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('btn.innerHTML = "<svg', "btn.innerHTML = '<svg")
text = text.replace('></svg>";', "></svg>';")

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(text)
print('Fixed syntax error')
