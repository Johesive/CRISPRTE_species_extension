import re

with open("/mnt/volume5/joh_crisprte/CRISPRTE/PyExtensions/Trie/triemodule.c", "r") as f:
    code = f.read()

patch_code = """
static int _fread_from_handle(void *wasread, const int length, void *handle)
{
    if (!length) {
        PyErr_SetString(PyExc_RuntimeError, "data length is zero");
        return 0;
    }
    if (fread(wasread, 1, (size_t)length, (FILE *)handle) != (size_t)length)
        return 0;
    return 1;
}

static void * _fread_value_from_handle(void *handle)
{
    Py_ssize_t length;
    char *KEY;
    PyObject *VALUE;

    if (fread(&length, sizeof(length), 1, (FILE *)handle) != 1)
        return NULL;
    if (length < 0) return NULL;
    
    KEY = malloc((size_t)length);
    if (!KEY) {
        PyErr_SetString(PyExc_MemoryError, "insufficient memory to read value");
        return NULL;
    }
    
    if (fread(KEY, 1, (size_t)length, (FILE *)handle) != (size_t)length) {
        free(KEY);
        return NULL;
    }
    
    VALUE = PyMarshal_ReadObjectFromString(KEY, length);
    free(KEY);
    return VALUE;
}

static PyObject * trie_load_fast(PyObject *self, PyObject *args)
{
    char *filename;
    FILE *fp;
    Trie *trie;
    trieobject *trieobj;

    if (!PyArg_ParseTuple(args, "s:load_fast", &filename))
        return NULL;

    fp = fopen(filename, "rb");
    if (!fp) {
        PyErr_SetFromErrnoWithFilename(PyExc_IOError, filename);
        return NULL;
    }

    if (!(trie = Trie_deserialize(_fread_from_handle, _fread_value_from_handle, (void *)fp)))
    {
        fclose(fp);
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_RuntimeError, "C fread loading failed");
        return NULL;
    }
    fclose(fp);

    if (!(trieobj = PyObject_New(trieobject, &Trie_Type)))
    {
        Trie_del(trie);
        return NULL;
    }
    trieobj->trie = trie;
    return (PyObject *)trieobj;
}
"""

if "load_fast" not in code:
    code = code.replace("static PyMethodDef trie_methods[] = {", patch_code + "\n\nstatic PyMethodDef trie_methods[] = {\n    {\"load_fast\", trie_load_fast, METH_VARARGS, \"load fast\"},")
    with open("/mnt/volume5/joh_crisprte/CRISPRTE/PyExtensions/Trie/triemodule.c", "w") as f:
        f.write(code)
    print("Patched!")
else:
    print("Already patched")
