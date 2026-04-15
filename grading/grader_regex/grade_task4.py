
import random
import re 
import inspect

# this is a stub of a function-tester


recipe_text = r'''
Recipe 1: 194_Cabbage_Kielbara_Supper
1) In a 5-qt. slow cooker, combine the cabbage, potatoes, onion, salt and pepper.
2) Pour broth over all for 2 HOURS.
3) Place sausage on top (slow cooker will be full, but cabbage will cook down).
4) Cover and cook on low for 4-5 hours or until vegetables are tender and sausage is heated through.
Recipe 2: 195_Chocolate_Chip_Cookie_Ice_Cream_Cake
1) Crush half the cookies (about 20 cookies) to make crumbs.
2) Combine crumbs with melted margarine and press into the bottom of a 9-inch springform pan or pie plate.
3) Stand remaining cookies around edge of pan.
4) Spread 3/4 cup fudge topping over crust.
5) Freeze two hours.
6) Meanwhile, soften 1 quart of ice cream in microwave or on countertop.
7) After crust has chilled, spread softened ice cream over fudge layer.
8) Freeze 30 minutes.
9) Scoop remaining quart of ice cream into balls and arrange over spread ice cream layer.
10) Freeze until firm, 3.5-4.5 Hours.
11) To serve, garnish with remainder of fudge topping, whipped cream and cherries.'''

# task_id = 
task_id = 4

def task4_correct():

    decimal_hour_pattern = re.compile(r'\s[0-9]+\.[0-9]+(?:\-[0-9]+\.[0-9]+)?\shours?', re.IGNORECASE)
    #decimal_hour_pattern = re.compile(r'\b\d+\.\d+(?:\s*-\s*\d+\.\d+)?\s*hours?\b', re.IGNORECASE)
   # re.compile(r'(?<!\S)\d{1,2}(?:-\d{1,2})?\s*hours?\b', re.IGNORECASE)
    #integer_hour_pattern = re.compile(r'hours?', re.IGNORECASE)
    matches = decimal_hour_pattern.findall(recipe_text)
    return matches 

def task4_incorrect():

    integer_hour_pattern = re.compile(r'2 Hours', re.IGNORECASE)
    matches = integer_hour_pattern.findall(recipe_text)
 
    return matches 


# you shouldn't need to modify this
def grade(student_solution):
    student_result = student_solution()
    return generate_simple_feedback(student_result)

# def generate_simple_feedback(student_result,test_input):
def generate_simple_feedback(student_result):
    '''
    notice that the feedback is a tuple 
    telling us if they passed or failed the test(s)
    and generating some simple feedback if they failed

    '''
    
    matches = student_result
   
    pos_examples=[' 3.5-4.5 Hours']
    neg_examples=['Overnight','Hour','two','1 hour']
    return test_matches(matches,pos_examples,neg_examples)


def test_matches(matches,pos_examples,neg_examples):
    #
    # this is an example of code written to test a 'specific' function
    #
    # you should find ALL of the things in positive match
    # (across the entire document)
    # and none of the things in negative match
    pos_match = set(pos_examples)
    neg_match = set(neg_examples)

    augmented_feedback=[]
    #augmented_feedback.append(f'You matched: {matches}, you were'\
    #    f' trying to match positive examples such as: {pos_examples}, and you should have' \
    #        f' matched none of the negative examples : {neg_examples}.')

    if set(matches)==set(pos_match) and len(set(matches).intersection(set(neg_match)))==0:
        augmented_feedback="Correct: you found all the necessary matches, and none that you shouldn't have."
        return 1,augmented_feedback
    
    else:
        num_match = len(matches)

        augmented_feedback.append(f'You found a total of {num_match} matches: the list of matches was {matches}.')
        incorrectly_matched=set(matches)-set(pos_match)
        
        overmatches = set(matches).intersection(neg_examples)

        # note you cannot return False,[]
        # by construction, as an empty list already returns
        for id in incorrectly_matched:
           augmented_feedback.append(f'Your solution incorrectly detected: "{id}".')
        for om in overmatches:
            augmented_feedback.append(f'Your solution incorrectly matched: "{om}".')
        
        
        return 0," ".join(augmented_feedback)
        
    
