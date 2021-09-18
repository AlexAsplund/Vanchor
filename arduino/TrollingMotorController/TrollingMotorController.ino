#include <AccelStepper.h>
#include <math.h>
#include <CmdBuffer.hpp>
#include <CmdCallback.hpp>
#include <CmdParser.hpp>

const String version = "v0.0.4";

///////////////////////////////
// Options
///////////////////////////////
// Communication
const int baudRate = 19200;


///////////////////////////////
// Steering
///////////////////////////////

const double stepsPerRevolution = 200;



// Stepper pinout
const int stepperA1 = 8;
const int stepperA2 = 9;
const int stepperB1 = 10;
const int stepperB2 = 11;
const int stepperDefaultAcceleration = 460;
const int stepperCalibrationSensorPin = 5;
bool stepperCalibrateSensorUndetectedState = true; // state of calibration sensor in undetected state
const int stepperDefaultSpeed = 180;

///////////////////////////////
// Trolling motor controller
///////////////////////////////
const int trollingMotorPwm = 3; // Pin for enabling trolling motor
const int trollingMotorFw = 6; // Run trolling motor forward
const int trollingMotorRev = 7; // Run trolling motor reverse

const int trollingMotorFwReverseDelay = 900; // ms (delay between reverse and forwarding, to not destroy the engine)
int rampTime = 800; //ms
// Other
const int outputDelay = 500; // ms


// Init vars
double long lastOutput = millis();
bool chokeMotor;
bool output = true;
int currentMotorSpeed;
int motorSpeed;
int lastMotorSpeed;
long double rampStart;
int rampValue;
bool ramping;
bool rampBlock;
bool motor_forward;

int calibBegin;
int calibEnd;

int set_stepperPosition;
int set_stepperSpeed;
int set_stepperAcceleration;
int set_motorSpeed;
bool set_motorRev;

///////////////////////////////
// INIT
///////////////////////////////

// Stepper
AccelStepper stepper(AccelStepper::FULL4WIRE, stepperA1, stepperA2, stepperB1, stepperB2);

// CMD
CmdCallback<3> cmdCallback;
CmdBuffer<32> myBuffer;
CmdParser     myParser;


// Define commands

char strUpdate[] = "UPD";
char strCalib[] = "CAL";
char strPing[] = "PING";


// Init functions


void enableMotor(bool reverse=false){
  digitalWrite(trollingMotorFw, reverse != true);
  digitalWrite(trollingMotorRev, reverse);
}


void setup() {

  // Change PWM frequency on PIN3 / PIN11 to 31372.55Hz
  TCCR2B = TCCR2B & B11111000 | B00000001;
  
  Serial.begin(baudRate);

  // Set pins
  Serial.println("SETUP: pinmode");
  pinMode(stepperCalibrationSensorPin, INPUT);

  // CMD
  lastOutput = millis();

  Serial.println("SETUP: CMD");
  myBuffer.setEcho(true);
  
  
  cmdCallback.addCmd(strPing, &replyPing);
  cmdCallback.addCmd(strCalib, &calibCmd);
  cmdCallback.addCmd(strUpdate,&updateCmd);
}


void loop()
{

  if(!Serial) {  //check if Serial is available... if not,
      Serial.end();      // close serial port
      delay(100);        //wait 100 millis
      Serial.begin(baudRate); // reenable serial again
  }
  else{
    stepper.run();
  
    rampMotor();

    if(stepper.distanceToGo() == 0){
      stepper.disableOutputs();
    }
            
    if(digitalRead(stepperCalibrationSensorPin)){
      calibBegin = stepper.currentPosition(); 
    }
    
    if(digitalRead(stepperCalibrationSensorPin)){
      calibEnd = stepper.currentPosition();
    }

    cmdCallback.updateCmdProcessing(&myParser, &myBuffer, &Serial);
  }
}

void setMotorSpeed(int speed, bool reverse=false){

  
  if(motorSpeed < speed && (reverse && motor_forward == true) != true && (reverse == false && motor_forward == false) != true){
    analogWrite(trollingMotorPwm, motorSpeed*2.5);
  }

  lastMotorSpeed = motorSpeed;
  motorSpeed = speed;
  
  if(reverse && motor_forward == true){
    motor_forward = false;
    ramping = false;
    analogWrite(trollingMotorPwm, 0);
    
    digitalWrite(trollingMotorFw, false);
    digitalWrite(trollingMotorRev, true);
    
    rampStart = millis() + trollingMotorFwReverseDelay;
  }
  else if(reverse == false && motor_forward == false){
    motor_forward = true;
    ramping = false;

    analogWrite(trollingMotorPwm, 0);

    digitalWrite(trollingMotorFw, true);
    digitalWrite(trollingMotorRev, false);
    
    rampStart = millis() + trollingMotorFwReverseDelay;
  }
  else {
    motor_forward = true;
    rampStart = millis();
  }

  Serial.print("rampMotor | Starting motor ramp at ");
  Serial.println(millis());
  
}

void rampMotor(){

  
  if(millis() < (rampStart + rampTime)){
    ramping = true;
    
    int rampValue = round((100.0 / rampTime) * (millis() - rampStart));
    int newSpeed = motorSpeed;
    

    if(lastMotorSpeed < motorSpeed){
      newSpeed = lastMotorSpeed + rampValue;
      if(newSpeed > motorSpeed){
        newSpeed = motorSpeed;
      }
    }
    else if(lastMotorSpeed == motorSpeed){
      newSpeed = motorSpeed;
    }
    else{
      newSpeed = lastMotorSpeed - rampValue;
      if(newSpeed < motorSpeed){
        newSpeed = motorSpeed;
      }
    }

    analogWrite(trollingMotorPwm, newSpeed*2.5);
    currentMotorSpeed = newSpeed;

  }
  else {
    if(ramping){
      ramping = false;
      currentMotorSpeed = motorSpeed;
      analogWrite(trollingMotorPwm, motorSpeed*2.5);
      Serial.print("rampMotor | Ending motor ramp at ");
      Serial.println(millis());
    }
  }
}

void replyPing(CmdParser *myParser){
  Serial.println("Pong");
  Serial.println("OpenTrollingMotor Controller "+version);
}

void calibCmd(CmdParser *myParser){
  stepper.setCurrentPosition(atoi(myParser->getCmdParam(1)));
}

int cstepperPosition;
int cstepperSpeed;
int cstepperAcceleration;
int cmotorSpeed;
bool cmotorRev;

void updateCmd(CmdParser *myParser){
  cstepperPosition = atoi(myParser->getCmdParam(1));
  cstepperSpeed = atoi(myParser->getCmdParam(2));
  cstepperAcceleration = atoi(myParser->getCmdParam(3));
  cmotorSpeed = atoi(myParser->getCmdParam(4));
  cmotorRev = atoi(myParser->getCmdParam(5)) == 1;
  
  
  if(set_stepperPosition != cstepperPosition){
    stepper.moveTo(cstepperPosition);
    set_stepperPosition = cstepperPosition;
  }
  
  if(set_stepperSpeed != cstepperSpeed){
    set_stepperSpeed = cstepperSpeed;
    stepper.setMaxSpeed(cstepperSpeed);
  }
  
  if(set_stepperAcceleration != cstepperAcceleration){
    set_stepperAcceleration = cstepperAcceleration;
    stepper.setAcceleration(cstepperAcceleration);
  }
  
  if(set_motorSpeed != cmotorSpeed){
    setMotorSpeed(cmotorSpeed, cmotorRev);
    set_motorSpeed = cmotorSpeed;
    set_motorRev = cmotorRev;
  }

  Serial.print("STATUS ");
  Serial.print("SSP:");
  Serial.print(set_stepperPosition);
  Serial.print(" SDTG:");
  Serial.print(stepper.distanceToGo());
  Serial.print(" MS:");
  Serial.print(set_motorSpeed);
  Serial.print(" CB:");
  Serial.print(calibBegin);
  Serial.print(" CE:");
  Serial.println(calibEnd);
}
