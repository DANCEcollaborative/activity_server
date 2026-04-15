
import random
import re 
import inspect

# positive test cases 
# negative test cases

positive_test_cases=['Recipe 3:','Recipe 99:']
negative_test_cases=['Recipe by itself',
                     'Recipe 5 but no colon']
# task_id = 
task_id = 6

def apply_student_regex_to_text(student_regex,recipe_text):

    srt = re.split(student_regex, recipe_text.strip())
    return [s for s in srt if s]

def task6_correct():
    recipe_pattern_lookahead = re.compile(r'(?=Recipe \d+:)')
    return recipe_pattern_lookahead

def task6_incorrect():
    return  re.compile(r'Recipe \d+:')

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
    over_group=False
    
    unmatched=[]
    incorrectly_matched=[]
    group_contents=[]
    # check correctness 

    for pos_example in positive_test_cases:
        
        matched_group =compiled_expression.search(pos_example).group()
        
        if not(compiled_expression.search(pos_example)):
            pos_match=False
            unmatched.append(pos_example)
            
        print(matched_group)
        print(len(matched_group))
        if len(matched_group)>0:
            group_contents.append(f'When matching {pos_example} your match is capturing or grouping the characters "{matched_group}". ')
            over_group=True
    
    for neg_example in negative_test_cases:
        if compiled_expression.search(neg_example):
            neg_match=True
            incorrectly_matched.append(neg_example)    
        
    if pos_match and not(neg_match) and not(over_group):
        return(1, "Congratulations! You have correctly completed the regular expression.")
    if over_group:
        return(0, "".join(group_contents))
    elif pos_match and neg_match:
        return(0, f'The student matches too many things. For example you matched "{random.choice(incorrectly_matched)}" but should not have.')
    elif not pos_match:
        return(0, f'The student does not match the positive cases. For example you did not match "{random.choice(unmatched)}".')
