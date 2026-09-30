#Function to generate a 4-digit pin code for clocking in and clocking out 
import random 

#generate random 4 digit pin code
def randomCODE():
  pinCODE = random.randint(0000, 9999)
  return pinCODE
