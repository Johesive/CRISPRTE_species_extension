with open('/mnt/volume5/joh_crisprte/CRISPRTE/src/trie2db.py', 'r') as f:
    text = f.read()

old_code = '''        if valid_inputs:
            az = predict(np.array(valid_inputs))
            for v_idx, score in zip(valid_indices, az):
                rows[v_idx][6] = score'''

new_code = '''        if valid_inputs:
            az = []
            for b_start in range(0, len(valid_inputs), 10000):
                b_end = min(len(valid_inputs), b_start + 10000)
                az.extend(predict(np.array(valid_inputs[b_start:b_end])))
            for v_idx, score in zip(valid_indices, az):
                rows[v_idx][6] = score'''

with open('/mnt/volume5/joh_crisprte/CRISPRTE/src/trie2db.py', 'w') as f:
    f.write(text.replace(old_code, new_code))
