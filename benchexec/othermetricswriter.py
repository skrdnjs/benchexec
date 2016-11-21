# prepare for Python 3
from __future__ import absolute_import, division, print_function, unicode_literals

import csv

class Othermetricswriter(object):

    def __init__(self, runSet):

        self.runSet = runSet
        self.fieldnames = ["FileName","NoR","RLen","RLeninBlkAvg","AFC","SFC","ComS","TfR","TTfCPA","TfFC","TfTran","TfSMTwoitp"]
        self.filename = runSet.log_folder+"csv"

    def other_before_runset(self):

        print(self.filename)

        f = open(self.filename, 'w')
        writer = csv.DictWriter(f, fieldnames=self.fieldnames)
        writer.writeheader()

        f.close()


    def other_after_run(self, run):

        print(run.log_file)

        f = open(run.log_file,"r")
        lines = f.readlines()
        f.close()

        dic = {}

        dic[self.fieldnames[0]] = run.identifier

        # loop for extracting other metrics

        for line in lines:
            if line.find("Number of successful refinements:") >= 0:
                tokens = line.split()
                dic["NoR"] = tokens[len(tokens)-1]

            if line.find("Length of refined path (in blocks):") >= 0:
                tokens = line.split()
                dic["RLen"] = tokens[6]
                rtoken = tokens[len(tokens)-1]
                dic["RLeninBlkAvg"] = rtoken[0:len(rtoken)-1]

            if line.find("Attempted forced coverings:") >= 0:
                tokens = line.split()
                dic["AFC"] = tokens[len(tokens)-1]

            # successful forced covering can be none if attempted forced covering is zero
            # if no successful forced covering, how?
            if line.find("Successful forced coverings:") >= 0: 
                tokens = line.split()
                dic["SFC"] = tokens[3]

            if line.find("Number of computed successors:") >= 0:
                tokens = line.split()
                dic["ComS"] = tokens[len(tokens)-1]

            if line.find("Time for refinement:") >= 0:
                tokens = line.split()
                rtoken = tokens[len(tokens)-1]
                dic["TfR"] = rtoken[0:len(rtoken)-1]

            if line.find("Total time for CPA algorithm:") >= 0:
                tokens = line.split()
                rtoken = tokens[5]
                dic["TTfCPA"] = rtoken[0:len(rtoken)-1]

            if line.find("Time for forced covering:") >= 0:
                tokens = line.split()
                rtoken = tokens[4]
                dic["TfFC"] = rtoken[0:len(rtoken)-1]

            if line.find("Time for transfer relation:") >= 0:
                tokens = line.split()
                rtoken = tokens[4]
                dic["TfTran"] = rtoken[0:len(rtoken)-1]

            if line.find("Total time for SMT solver (w/o itp):") >= 0:
                tokens = line.split()
                rtoken = tokens[7]
                dic["TfSMTwoitp"] = rtoken[0:len(rtoken)-1]

        for field in self.fieldnames:
            if dic.get(field) is None:
                dic[field] = "none"

        f = open(self.filename, 'a')
        writer = csv.DictWriter(f, fieldnames=self.fieldnames)
        writer.writerow(dic)
        f.close()


    def other_after_runset(self):

        pass
