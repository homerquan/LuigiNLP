import luiginlp
from luiginlp.modules.frog import Frog
from luiginlp.engine import Parallel
luiginlp.run(Parallel(component='Frog',inputfiles="test.rst,test.rst2"))