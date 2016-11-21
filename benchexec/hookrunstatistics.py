# prepare for Python 3
from __future__ import absolute_import, division, print_function, unicode_literals

from benchexec.othermetricswriter import Othermetricswriter

def hookrunstatistics(run, other_writer):

    print(run.log_file)

    f = open(run.log_file,"r")
    lines = f.readlines()
    f.close()

    for line in lines:
        if line.find("CEGAR algorithm statistics") >= 0: # CEGAR algorithm is used
            print("Hey! I found the CEGAR statistics!: ",line)
