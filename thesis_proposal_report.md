# Thesis Proposal Report: AI-Driven Human-Centric Lighting in Office Environments

## Working Title

**Evaluating User Experience and Visual Comfort in AI-Driven Human-Centric Lighting Systems: A Case Study in Office Environments**

## Purpose of the Study

This thesis proposes an applied field study of an AI-driven adaptive lighting system in a real office environment. The core problem is that modern luminaires often contain advanced technical capabilities, such as DALI control, tunable-white light, presence sensing, and daylight-responsive operation, but these capabilities are not always used effectively in daily workplace operation.

The proposed study investigates whether an existing office luminaire system can be improved through an AI-based control layer without changing the main hardware infrastructure. The focus is not only on energy-related operation, but also on visual comfort, user satisfaction, and human-centric lighting behavior.

## Research Motivation

Office lighting systems increasingly support dynamic control of brightness and correlated color temperature. In practice, however, luminaires may remain switched on while desks are unoccupied, or stay at unnecessarily high brightness levels even when daylight is sufficient. This creates a gap between available lighting technology and actual operational performance.

The thesis positions AI as a possible solution to this gap. Unlike fixed rule-based strategies, an AI-driven controller can combine multiple contextual inputs, such as occupancy, ambient illuminance, time of day, and learned user behavior, to determine more suitable lighting settings over time.

## Main Research Question

To what extent can an AI-driven adaptive control approach improve the operational efficiency and user-centered performance of an existing office luminaire system under real working conditions?

## Sub-Questions

- How does existing manual lighting operation compare to AI-driven adaptive control in terms of runtime behavior, dimming profiles, and estimated energy consumption?
- Does AI-based adaptive control influence user perception of lighting quality, comfort, and satisfaction?
- Can measurable improvements be achieved without modifying the existing hardware infrastructure?

## Proposed Experimental Design

The thesis follows a two-phase comparative field study in an operational office setting.

### Phase 1: Baseline Condition

In the baseline phase, the luminaire operates under its existing manual user-controlled configuration. The system passively records operational and environmental data, including occupancy, illuminance, dimming level, CCT, activation state, and runtime behavior.

This phase documents how the luminaire is normally used and provides a reference condition for later comparison. It also supports identification of inefficiencies such as lighting during absence or high artificial light output during sufficient daylight.

### Phase 2: AI-Driven Adaptive Control

In the AI-driven phase, an adaptive control model is introduced while keeping the same physical office, luminaire, sensors, and DALI infrastructure. The decision-making layer changes from manual operation to an AI-supported controller.

The controller processes contextual inputs such as occupancy, ambient illuminance, and time-related variables. Based on these inputs, it adjusts brightness and correlated color temperature. The AI model is intended to learn from baseline operation and produce lighting settings that better match environmental conditions and user preferences.

## Prototype System Architecture

The proposed setup consists of three functional layers:

| Layer | Components | Role |
| --- | --- | --- |
| Sensing | ESP32 Dev Kit C, LD2410C radar presence sensor, BH1750 illuminance sensor | Measures occupancy and ambient lux |
| Decision-making | PC-based control software | Logs data and applies manual or AI-driven control logic |
| Actuation | Tridonic USB-DALI interface, DALI DT8 floor luminaire | Applies brightness and CCT commands to the luminaire |

The sensing and actuation paths are separated. Sensor data are transmitted to the PC through USB serial communication, while lighting commands are sent from the PC through the USB-DALI interface to the luminaire. This makes it possible to evaluate changes in the control strategy while keeping the physical installation stable.

## AI Control Logic

The thesis proposes that the AI controller should not adjust the light continuously. Instead, sensor data are recorded at short intervals, while AI control evaluations occur at defined intervals to avoid distracting micro-adjustments.

The draft methodology defines the following timing and threshold configuration:

| Process | Interval or condition | Purpose |
| --- | --- | --- |
| Sensor data sampling | 5 seconds | Record occupancy and illuminance changes |
| AI control evaluation | 5 minutes | Decide whether brightness or CCT should change |
| Absence timeout | 1 minute | Switch off after sustained vacancy |
| Brightness threshold | More than 5% change | Avoid unnecessary small brightness changes |
| CCT threshold | More than 100 K change | Avoid unnecessary small color-temperature changes |

## Data Collection

The proposed study records the same parameters in both phases so that the baseline and AI-driven conditions can be compared directly.

| Category | Parameter | Source |
| --- | --- | --- |
| Environmental data | Occupancy status | LD2410C radar presence sensor |
| Environmental data | Ambient illuminance in lux | BH1750 light sensor |
| Luminaire operation | Dimming level | PC control software |
| Luminaire operation | Correlated color temperature | PC control software |
| Luminaire operation | Activation status | PC control software |
| Luminaire operation | Runtime duration | PC control software |

User perception is proposed to be evaluated through structured questionnaires in both phases. The questionnaire should assess lighting quality, visual comfort, perceived stability, and overall satisfaction. The second phase may also include a comparative question asking whether the participant preferred manual or adaptive control.

## Evaluation Metrics

The thesis proposes three main evaluation areas:

1. **Operational performance:** luminaire runtime, lighting during absence, dimming behavior, and CCT behavior.
2. **Estimated energy performance:** energy-related comparison based on runtime, dimming level, and nominal luminaire power, since direct electrical measurement is not included.
3. **User perception and comfort:** Likert-scale questionnaire results comparing subjective comfort and satisfaction between baseline and AI-driven operation.

The analysis is intended to focus on relative differences between the two phases rather than broad statistical generalization. This is appropriate because the setup uses a prototype system, a limited number of participants, and a single real office installation.

## Connection to the App-Generated Telemetry

The current KoL-v2 app telemetry structure already supports much of the proposed methodology. The AI telemetry file generated by the application includes occupancy state, ambient lux, lamp state, brightness level, CCT, runtime, control actions, and explanatory rationale fields.

This means the application is not only controlling the luminaire but also producing data suitable for thesis analysis. The telemetry can support later sections on runtime behavior, dimming behavior, CCT adaptation, vacancy response, and explainability of AI decisions.

## Expected Contribution

The proposed thesis contributes an applied evaluation of AI-driven adaptive lighting in an existing office context. Its main value is that it examines whether smarter control can be added through the decision-making layer rather than through a complete hardware redesign.

The expected contribution is therefore practical and methodological:

- It evaluates AI lighting control under real office conditions.
- It compares adaptive control against normal manual operation.
- It connects technical performance with user comfort.
- It documents whether existing DALI luminaires can be enhanced through software-driven intelligence.
- It provides a transparent logging approach for later validation and auditability.

## Limitations

The proposal acknowledges several limitations. The setup is a prototype rather than a finished commercial integration. Energy consumption is estimated rather than directly measured. The study focuses on a limited participant group and a single luminaire installation, which limits generalizability. Finally, comfort and satisfaction are measured through self-reported questionnaires, so the results may be influenced by individual expectations and subjective preference.

## Short Conclusion

The thesis proposes a realistic and useful evaluation of AI-driven human-centric lighting. The central idea is to test whether an existing DALI office luminaire can become more efficient, comfortable, and responsive by changing the control strategy rather than replacing the hardware. The KoL-v2 application and its telemetry output appear well aligned with this methodology, especially because they capture both operational measurements and explainable AI decision traces.
