# KDL.py

A handwritten [KDL](https://kdl.dev) parser in Python 3.7+,
which is fully compliant with KDL 1.0.0.

## Usage

```py3
import kdl

doc = kdl.parse('''
node_name "arg" {
	child_node foo=1 bar=true
}
''')
print(doc.values[0])