
#pragma once

#include <Arduino.h>
#include <deque>

namespace loopTimer {

// a class which is used to compute exponential running average of values.  As a special case, it can compute the
// average running time between events.  
class RunningAverage {
   public:
    void init(float alpha, float value) {
        this->alpha = alpha;
        this->average = value;
    }

    RunningAverage(float alpha) {
        float time = (float) millis();
        this->lastEventTime = time;
        init(alpha, 0);
    }

    RunningAverage(float alpha, float value) : lastEventTime(0) {
        init(alpha, value);
    }

    void push(float value) {
        average = (average * alpha) + (value * (1 - alpha));
    }

    // push the time since the last event
    void event() {
        float now = (float) millis();
        push(now - lastEventTime);
        lastEventTime = now;
    }   

    float getAverage() const { return average; }

   private:
    float alpha;
    float average;
    float lastEventTime;
};

class MinMaxQueue {
private:
    std::deque<float> data; // Stores the last n numbers
    std::deque<float> minDeque; // Stores potential minimum values in non-decreasing order
    std::deque<float> maxDeque; // Stores potential maximum values in non-increasing order
    int n; // Size of the window

public:
    MinMaxQueue(int size) : n(size) {}

    void push(float value) {
        data.push_back(value);

        // Maintain minDeque: the list of items which are smaller than the last element.
        while (!minDeque.empty() && minDeque.back() > value) {
            minDeque.pop_back();
        }
        minDeque.push_back(value);

        // Maintain maxDeque: the list of items which are larger than the last element.
        while (!maxDeque.empty() && maxDeque.back() < value) {
            maxDeque.pop_back();
        }
        maxDeque.push_back(value);

        // If the size of data exceeds n, remove the oldest element
        if (data.size() > n) {
            if (data.front() == minDeque.front()) {
                minDeque.pop_front();
            }
            if (data.front() == maxDeque.front()) {
                maxDeque.pop_front();
            }
            data.pop_front();
        }
    }

    float getMin() const {
        return minDeque.front(); // The front of minDeque is the minimum value in the window
    }

    float getMax() const {
        return maxDeque.front(); // The front of maxDeque is the maximum value in the window
    }
};

class RunningMinMax {
private:
    MinMaxQueue minMaxQueue;
    float lastEventTime;
public:
    RunningMinMax(int size) : minMaxQueue(size) {
        float time = (float) millis();
        lastEventTime = time;
        push(0);
    }

    RunningMinMax(int size, float value) : minMaxQueue(size), lastEventTime(0) {
        push(value);
    }

    void event() {
        float time = (float) millis();
        minMaxQueue.push(time - lastEventTime);
        lastEventTime = time;
    }

    void push(float value) {
        minMaxQueue.push(value);
    }

    float getMin() const {
        return minMaxQueue.getMin();
    }

    float getMax() const {
        return minMaxQueue.getMax();
    }
};

// running stats combines a running average and a running minmax
class RunningStats {
private:
    RunningAverage runningAverage;
    RunningMinMax runningMinMax;
    float lastEventTime;
public:
    RunningStats(float alpha, int size) : runningAverage(alpha), runningMinMax(size) {
    }

    RunningStats(float alpha, int size, float value) : runningAverage(alpha), runningMinMax(size) {
        push(value);
    }

    void event() {
        runningAverage.event();
        runningMinMax.event();
    }

    void push(float value) {
        runningAverage.push(value);
        runningMinMax.push(value);
    }

    float getAverage() const {
        return runningAverage.getAverage();
    }

    float getMin() const {
        return runningMinMax.getMin();
    }

    float getMax() const {
        return runningMinMax.getMax();
    }
};

}  // namespace loopTimer