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
task_id = 5

# positive test cases 
# negative test cases

positive_test_cases=['Recipe 3:','Recipe 99:']
negative_test_cases=['Recipe by itself',
                     'Recipe 5 but no colon']
# implement both a correct and an incorrect solution
def task5_incorrect():

    return re.compile(r'Recipe', re.IGNORECASE)

# correct solution
def task5_correct():
    return re.compile(r'(?=Recipe \d+:)')
    #\b\d+\.\d+(?:\s*-\s*\d+\.\d+)?\s*hours?\b', re.IGNORECASE)
    #return re.compile(r'(?<!\S)\d{1,2}(?:-\d{1,2})?\s*hours?\b', re.IGNORECASE)
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
        return(1, "Congratulations! You have correctly completed the regular expression.")
    elif pos_match and neg_match:
        return(0, f'The student matches too many things. For example you matched "{random.choice(incorrectly_matched)}" but should not have.')
    elif not pos_match:
        return(0, f'The student does not match the positive cases. For example you did not match "{random.choice(unmatched)}".')

def task_3b_correct(recipe_text):
    spaced_regex_pattern = re.compile(r'(?=Recipe \d+:)')
    #spaced_regex_pattern = re.compile(r'(?=Recipe)')
    srt = re.split(spaced_regex_pattern, recipe_text.strip())
    return [ s for s in srt if s]

def task_3b_incorrect(recipe_text):
    return []

def test_split_function(split_recipes):
    
    feedback_prompt=[f'The student wrote this function {inspect.getsource(split_recipes)} '
        ' to split a file up by individual recipes.']
    is_Correct=True
    
    with open('./recipe_test_file.txt', 'r') as file:
        test_string = file.read()

    strings_to_evaluate = split_recipes(test_string)

    # make sure you are splitting
    if len(strings_to_evaluate)==1:
        feedback_prompt.append('The function only returned one string, make sure you are using the right method to split.')
        is_Correct = False
    elif len(strings_to_evaluate)==4:
        feedback_prompt.append('The function got the correct number of recipes.')
    else:
        feedback_prompt.append('The function returned an incorrect number of recipes.')
        is_Correct = False
    # make sure you don't lose the recipe
    for recipe in strings_to_evaluate:
        if re.search("Recipe",recipe):
            pass
        else:
            feedback_prompt.append('The recipe does not start with recipe, did you remove it when you split?')
            is_Correct = False
    
    if re.search('browned.$',strings_to_evaluate[0]):
        feedback_prompt.append('The first recipe includes the correct ending.')
        is_Correct = True
    else:
        feedback_prompt.append('The end of the first recipe is not correct.')
        is_Correct = False

    if is_Correct:
        feedback_prompt.append("The student's solution is correct. Ask them to write their own test(s)")


        
    return is_Correct,' '.join(feedback_prompt)