################################################################
class DictClass(dict):
    """
    Class that constructs like a dict, but acesss fields like a class.  Makes things much
    more compact.  So:

    foo = DictClass(one=1, two=2, bar=10)
    foo.bar (rather than foo['bar'])

    Note, it inherits from dict, so it can do all the dict things as well!
    """
    def __init__(self, **args):
        for key in args.keys():
            self[key] = args[key]

    def __setattr__(self, attr, val):
        self[attr] = val
        
    def __getattr__(self, attr):
        return self[attr]

    def union(self, dict_like):
        "Like dict update, but returns a new DictClass.  Keys in the argument overwrite keys in self."
        res = DictClass(**self)
        for k, v in dict_like.items():
            res[k] = v
        return res
    
    def __str__(self):
        res = ""
        for k in sorted(self.keys()):
            v = self[k]
            if isinstance(v, float):
                res += f"{k}:{v:.2f}, "
            else:
                res += f"{k}:{v}, "
        return res

    def __deepcopy__(self, memo):
        return self.__class__(
            **{k: copy.deepcopy(v, memo) for k, v in self.items()}
        ) 
