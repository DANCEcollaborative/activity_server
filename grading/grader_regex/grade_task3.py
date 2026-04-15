# from test_utility import *
import numpy as np

import random
import re 
import inspect


# To update for a new task, you only need to update:
# task_id
# positive_test_cases, negative_test_cases
#
# task_id = 
task_id = 3

# positive test cases 
# negative test cases

positive_test_cases=[' 1.5 hours',' 3.5-4.5 Hours']
negative_test_cases=['Overnight','Hour','two','1 hour']
# implement both a correct and an incorrect solution
def task3_incorrect_regex():

    return re.compile(r'\s*hours?\b', re.IGNORECASE)

# correct solution
def task3_correct_regex():
    return re.compile(r'\s\b\d+\.\d+(?:\s*-\s*\d+\.\d+)?\s*hours?\b', re.IGNORECASE) 
    #re.compile(r'(?<!\S)\d{1,2}(?:-\d{1,2})?\s*hours?\b', re.IGNORECASE)
# this will test your feedback - if you are testing for 
# a wider range of errors, you need more test cases

# you shouldn't need to modify thie
def grade(student_solution):
    # return True, "Code update is being used"
    student_regex = student_solution()
    return generate_simple_feedback(student_regex,positive_test_cases,negative_test_cases)

def generate_simple_feedback(compiled_expression,positive_test_cases,negative_test_cases):
    '''
    notice that the feedback is a tuple 
    telling us if they passed or failed the test(s)

    and generating some simple feedback if they failed

    '''
    # status_string = f'''The student entered '{compiled_expression.pattern}'.'''

    #tudents_expression = re.compile(expression)
    pos_match=True
    neg_match=False
    
    unmatched=[]
    incorrectly_matched=[]
    # check correctness 
    for pos_example in positive_test_cases:
        if not(compiled_expression.match(pos_example)):
            pos_match=False
            unmatched.append(pos_example)
            
    for neg_example in negative_test_cases:
        if compiled_expression.match(neg_example):
            neg_match=True
            incorrectly_matched.append(neg_example)
            
    if pos_match and not(neg_match):
        return(1,"Congratulations! You have correctly completed the regular expression.")
    elif pos_match and neg_match:
        return(0, f'The student matches too many things. For example you matched "{random.choice(incorrectly_matched)}" but should not have.')
    elif not pos_match:
        return(0, f'The student does not match the positive cases. For example you did not match "{random.choice(unmatched)}".')
