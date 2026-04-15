
import random
import re 
import inspect

# this is a stub of a function-tester


recipe_text = r'''
Recipe 1: 194_Cabbage_Kielbasa_Supper
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
task_id = 8


def split_recipes(recipe_text):
    spaced_regex_pattern = re.compile(r'(?=Recipe \d+:)')
    #spaced_regex_pattern = re.compile(r'(.+Recipe \d+:)')
    #spaced_regex_pattern = re.compile(r'Recipe \d:')
    #spaced_regex_pattern = re.compile(r'(?=Recipe)')
    #print(re.findall(spaced_regex_pattern,recipe_text.strip()))
    srt = re.split(spaced_regex_pattern, recipe_text.strip())
    return [ s for s in srt if s]


def task8_correct():
    replace_regex_pattern = re.compile(r'two hours|2 hours',re.IGNORECASE)
    modded_texts=[]
    spaced_recipe_text = split_recipes(recipe_text)
    for recipe in spaced_recipe_text:
        organized_recipe_text = re.sub(replace_regex_pattern,'1.5 hours', recipe)
        modded_texts.append(organized_recipe_text)
    return modded_texts

def task8_incorrect():
    spaced_recipe_text = split_recipes(recipe_text)
    return spaced_recipe_text

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
    
    processed_recipes = student_result
    return test_matches(processed_recipes)
    

def test_matches(processed_recipes):
    #
    # this is an example of code written to test a 'specific' function
    #
    is_correct=1
        
    replace_regex_pattern = re.compile(r'two hours|2 hours',re.IGNORECASE)
    feedback = []
    substitute_pattern=re.compile('1.5 hours',re.I)

    for recipe,n  in zip(processed_recipes,range(0,len(processed_recipes))):
        if replace_regex_pattern.search(recipe):
            feedback.append(f'Recipe {n+1} still contains the target')
            is_correct = 0
        elif substitute_pattern.search(recipe):
            feedback.append(f'Recipe {n+1} correctly modified.')
        else:
            feedback.append('No sub made.')
            is_correct = 0
    return is_correct,feedback
    
